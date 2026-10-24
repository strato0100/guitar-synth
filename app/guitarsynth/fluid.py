"""Proceso FluidSynth con audio y MIDI por JACK."""

import logging
import shutil
import subprocess
import threading
import time

log = logging.getLogger("fluidsynth")

DEFAULT_SOUNDFONT = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


class FluidSynth:
    """
    Lanza `fluidsynth` como proceso hijo. El shell de FluidSynth queda
    conectado a una tubería por stdin para enviarle comandos (reverb,
    chorus...); si la app termina, la tubería se cierra y FluidSynth sale.
    Las notas no pasan por aquí: llegan por un puerto MIDI de JACK.
    """

    def __init__(self, soundfont=DEFAULT_SOUNDFONT, name="gsynth-fluid", gain=0.7):
        self.soundfont = soundfont
        self.name = name
        self.gain = gain
        self.process = None
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, client, samplerate, timeout=20.0):
        """Arranca FluidSynth y espera a que aparezcan sus puertos en JACK."""
        binary = shutil.which("fluidsynth")
        if not binary:
            raise RuntimeError("No se encuentra 'fluidsynth'. Instálalo con: sudo apt install fluidsynth")

        existing = self.find_ports(client)
        if existing:
            raise RuntimeError(
                f"Ya hay un FluidSynth '{self.name}' en JACK (¿otra instancia de la app?). "
                "Ciérrala o ejecuta: pkill -f gsynth-fluid")

        cmd = [
            binary,
            "-a", "jack", "-m", "jack",
            "-o", f"audio.jack.id={self.name}",
            "-o", f"midi.jack.id={self.name}-midi",
            "-o", "audio.jack.autoconnect=0",
            "-o", "synth.polyphony=96",
            "-r", str(int(samplerate)),
            "-g", str(self.gain),
            self.soundfont,
        ]
        log.info("Arrancando: %s", " ".join(cmd))
        self.process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._log_stderr, daemon=True).start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                raise RuntimeError("FluidSynth terminó al arrancar (revisa los mensajes anteriores)")
            ports = self.find_ports(client)
            if ports:
                return ports
            time.sleep(0.25)
        raise RuntimeError("FluidSynth no registró sus puertos en JACK a tiempo")

    def find_ports(self, client):
        """(puerto MIDI de entrada, [salidas de audio]) o None."""
        audio = client.get_ports(name_pattern=self.name, is_audio=True, is_output=True)
        midi = client.get_ports(name_pattern=self.name, is_midi=True, is_input=True)
        if not midi and self.running:
            # Algunas versiones ignoran midi.jack.id y usan el nombre por defecto
            midi = client.get_ports(name_pattern="fluidsynth-midi", is_midi=True, is_input=True)
        if not midi or len(audio) < 2:
            return None
        audio = sorted(audio, key=lambda p: p.name)[:2]
        return midi[0].name, [p.name for p in audio]

    def _log_stderr(self):
        process = self.process
        for line in process.stderr:
            line = line.strip()
            if line:
                log.info("%s", line)

    def command(self, text):
        """Envía un comando al shell de FluidSynth."""
        with self.lock:
            if not self.running:
                return
            try:
                self.process.stdin.write(text + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                log.warning("No se pudo enviar '%s' a FluidSynth", text)

    def set_reverb_room(self, room):
        self.command("reverb on")
        self.command(f"rev_setroomsize {room:.2f}")

    def stop(self):
        if not self.process:
            return
        try:
            self.command("quit")
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
