"""
Motores de síntesis intercambiables.

Cada motor es un proceso externo que se conecta a JACK: la app le envía
las notas por un puerto MIDI de JACK y recoge su audio por dos puertos.
Cambiar de motor o de archivo de sonido es parar un proceso y arrancar otro.

  fluidsynth   SoundFonts .sf2, General MIDI (128 instrumentos, efectos).
  sfizz        Instrumentos .sfz (un archivo = un instrumento), por sfizz_jack.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time

log = logging.getLogger("backends")

SCAN_DEPTH = 4


class Backend:
    id = ""
    label = ""
    kind = ""           # extensión de los archivos de sonido: sf2 | sfz
    setting = ""        # ajuste que guarda el archivo elegido
    gm = False          # General MIDI: cambio de programa y 128 instrumentos
    binary = ""
    client = ""         # nombre del cliente JACK del proceso
    install_hint = ""
    load_timeout = 60.0

    def __init__(self, gain=0.7, effects=True):
        self.gain = gain
        self.effects = effects      # reverb y chorus propios del motor (cuestan CPU)
        self.sound = None
        self.process = None
        self.lock = threading.Lock()

    @classmethod
    def available(cls):
        return shutil.which(cls.binary) is not None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def command_line(self, binary, samplerate, sound):
        raise NotImplementedError

    def midi_fallback_pattern(self):
        return None

    def start(self, client, samplerate, sound):
        """Arranca el proceso y espera a que registre sus puertos en JACK."""
        binary = shutil.which(self.binary)
        if not binary:
            raise RuntimeError(f"No se encuentra '{self.binary}'. {self.install_hint}")
        if self.find_ports(client):
            raise RuntimeError(
                f"Ya hay un cliente JACK '{self.client}' (¿otra instancia de la app?). "
                f"Ciérrala o ejecuta: pkill -f {self.client}")

        cmd = self.command_line(binary, samplerate, sound)
        log.info("Arrancando: %s", " ".join(cmd))
        self.sound = sound
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._log_stderr, args=(self.process,), daemon=True).start()

        deadline = time.monotonic() + self.load_timeout
        while time.monotonic() < deadline:
            if not self.running:
                self.process = None
                raise RuntimeError(f"{self.label} terminó al arrancar con "
                                   f"{os.path.basename(sound)} (ejecuta con --verbose para ver el motivo)")
            ports = self.find_ports(client)
            if ports:
                return ports
            time.sleep(0.25)
        self.stop()
        raise RuntimeError(f"{self.label} no registró sus puertos en JACK a tiempo")

    def find_ports(self, client):
        """(puerto MIDI de entrada, [dos salidas de audio]) o None."""
        pattern = "^" + re.escape(self.client)
        audio = client.get_ports(name_pattern=pattern, is_audio=True, is_output=True)
        midi = client.get_ports(name_pattern=pattern, is_midi=True, is_input=True)
        if not midi and self.running and self.midi_fallback_pattern():
            midi = client.get_ports(name_pattern=self.midi_fallback_pattern(),
                                    is_midi=True, is_input=True)
        if not midi or len(audio) < 2:
            return None
        audio = sorted(audio, key=lambda p: p.name)[:2]
        return midi[0].name, [p.name for p in audio]

    def _log_stderr(self, process):
        for line in process.stderr:
            line = line.strip()
            if line:
                log.info("%s: %s", self.id, line)

    def command(self, text):
        """Envía un comando al shell del proceso (si lo tiene)."""
        with self.lock:
            if not self.running:
                return
            try:
                self.process.stdin.write(text + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                log.warning("No se pudo enviar '%s' a %s", text, self.id)

    def set_reverb_room(self, room):
        """Sólo FluidSynth tiene controles de efectos propios."""

    def stop(self):
        process, self.process = self.process, None
        if not process:
            return
        try:
            if process.poll() is None:
                self.command_to(process, self.quit_command())
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    @staticmethod
    def command_to(process, text):
        try:
            process.stdin.write(text + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def quit_command(self):
        return "quit"


class FluidBackend(Backend):
    id = "fluidsynth"
    label = "FluidSynth"
    kind = "sf2"
    setting = "sf2"
    gm = True
    binary = "fluidsynth"
    client = "gsynth-fluid"
    install_hint = "Instálalo con: sudo apt install fluidsynth"

    def command_line(self, binary, samplerate, sound):
        fx = [] if self.effects else ["-R", "0", "-C", "0"]
        return [
            binary,
            "-a", "jack", "-m", "jack",
            *fx,
            "-o", f"audio.jack.id={self.client}",
            "-o", f"midi.jack.id={self.client}-midi",
            "-o", "audio.jack.autoconnect=0",
            "-o", "synth.polyphony=96",
            "-r", str(int(samplerate)),
            "-g", str(self.gain),
            sound,
        ]

    def midi_fallback_pattern(self):
        # Algunas versiones ignoran midi.jack.id y usan el nombre por defecto
        return "fluidsynth-midi"

    def start(self, client, samplerate, sound):
        ports = super().start(client, samplerate, sound)
        if self.effects:
            self.command("chorus on")
        return ports

    def set_reverb_room(self, room):
        if not self.effects:
            return
        self.command("reverb on")
        self.command(f"rev_setroomsize {room:.2f}")


class SfizzBackend(Backend):
    id = "sfizz"
    label = "sfizz (SFZ)"
    kind = "sfz"
    setting = "sfz"
    gm = False
    binary = "sfizz_jack"
    client = "gsynth-sfizz"
    install_hint = ("Instala sfizz (el programa sfizz_jack): busca el paquete 'sfizz' "
                    "de tu sistema o compílalo desde github.com/sfztools/sfizz")
    load_timeout = 120.0    # los instrumentos con muchas muestras tardan en cargar

    def command_line(self, binary, samplerate, sound):
        return [binary, "--client_name", self.client, "--num_voices", "64", sound]


BACKENDS = {cls.id: cls for cls in (FluidBackend, SfizzBackend)}
DEFAULT_ENGINE = FluidBackend.id


def _pretty(path):
    name = os.path.splitext(os.path.basename(path))[0]
    return name.replace("_", " ")


_INCLUDE = re.compile(r'^\s*#include\s+"([^"]+)"', re.IGNORECASE | re.MULTILINE)


def _included_sfz(path):
    """Archivos que este .sfz incluye con #include (se resuelven junto a él)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read(1_000_000)
    except OSError:
        return set()
    here = os.path.dirname(path)
    return {os.path.realpath(os.path.join(here, ref.replace("\\", "/")))
            for ref in _INCLUDE.findall(text)}


def scan_library(dirs):
    """
    Busca archivos .sf2 y .sfz en las carpetas dadas (y subcarpetas).
    Los .sfz que sólo son piezas incluidas por otro .sfz no se listan.
    """
    found = {cls.kind: {} for cls in BACKENDS.values()}
    for base in dirs:
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            continue
        base_depth = base.rstrip(os.sep).count(os.sep)
        for root, subdirs, files in os.walk(base, followlinks=True):
            if root.count(os.sep) - base_depth >= SCAN_DEPTH:
                subdirs[:] = []
            for name in files:
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext not in found:
                    continue
                full = os.path.join(root, name)
                path = os.path.realpath(full)
                if path in found[ext] or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path) / 1e6 if ext == "sf2" else None
                rel = os.path.relpath(full, base).split(os.sep)
                folder = rel[0] if len(rel) > 1 else os.path.basename(base.rstrip(os.sep))
                found[ext][path] = {"id": path, "name": _pretty(path),
                                    "size_mb": None if size is None else round(size, 1),
                                    "folder": folder}

    included = set()
    for path in found["sfz"]:
        included |= _included_sfz(path)
    for path in included:
        found["sfz"].pop(path, None)

    return {kind: sorted(items.values(), key=lambda s: (s["folder"].lower(), s["name"].lower()))
            for kind, items in found.items()}
