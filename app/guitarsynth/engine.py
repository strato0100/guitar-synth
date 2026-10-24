"""
Motor de la app: une JACK, el detector de notas y el motor de síntesis.

  system:capture_1 ──> guitar-synth:input ──> (hilo de análisis) ──┐
                                                                   │ MIDI
  guitar-synth:midi_out <──────────────────────────────────────────┘
          │
          └──> motor de síntesis (FluidSynth o sfizz) ──> system:playback_1/2

El motor de síntesis y su archivo de sonido (.sf2 / .sfz) se pueden cambiar
en caliente desde la web: se para el proceso y se arranca otro.

El callback de JACK sólo copia audio y escribe MIDI pendiente; todo el
análisis va en un hilo aparte. Toda la lógica de notas (qué suena, qué
hay que apagar) vive en ese hilo, así que el resto de la app sólo pide
cambios mediante banderas.
"""

import logging
import os
import queue
import sys
import threading
import time
from collections import deque

import jack
import numpy as np

from .backends import BACKENDS, DEFAULT_ENGINE, scan_library
from .dsp import Decimator, note_name
from .mono import MonoTracker
from .poly import PolyTracker
from .router import OutputRouter

log = logging.getLogger("engine")

MIDI_CHANNEL = 0


class Engine:
    def __init__(self, store, capture_port="system:capture_1", sound_dirs=(),
                 client_name="guitar-synth", synth_gain=0.7, effects=True):
        self.store = store
        self.capture_port = capture_port
        self.client_name = client_name
        self.sound_dirs = list(sound_dirs)
        self.synth_gain = synth_gain
        self.effects = effects
        self.xruns = 0              # ciclos de audio perdidos por JACK desde el arranque
        self.worst_xrun_ms = 0.0
        self.library = scan_library(self.sound_dirs)

        self.backend = None
        self.loading = False
        self.switch_lock = threading.Lock()
        self.settings_rev = 0       # sube cuando la app corrige ajustes por su cuenta
        self.notice = {"id": 0, "text": "", "error": False}

        self.audio_queue = queue.Queue(maxsize=1024)
        self.midi_pending = deque()
        self.running = threading.Event()
        self.jack_died = threading.Event()

        self.client = None
        self.router = None
        self.tracker = None
        self.sounding = {}          # nota detectada -> nota MIDI enviada
        self.live = {}
        self.error = None

        # Peticiones al hilo de análisis
        self._reset_request = False
        self._settings_request = True

    # --- arranque y parada ---------------------------------------------

    def start(self):
        # El callback de JACK (cada 1.3 ms a 96 kHz/128) necesita el GIL. Por
        # defecto Python lo cede cada 5 ms, lo que provocaría xruns mientras el
        # hilo de análisis trabaja; con 0.5 ms el callback casi nunca espera.
        sys.setswitchinterval(0.0005)

        self.client = jack.Client(self.client_name)
        self.inport = self.client.inports.register("input")
        self.midi_out = self.client.midi_outports.register("midi_out")
        self.samplerate = self.client.samplerate
        self.blocksize = self.client.blocksize
        self.decimator = Decimator(self.samplerate)

        self.client.set_process_callback(self._process)
        self.client.set_shutdown_callback(self._shutdown)
        self.client.set_xrun_callback(self._xrun)
        self.client.activate()

        captures = [p.name for p in self.client.get_ports(is_audio=True, is_output=True,
                                                          is_physical=True)]
        if self.capture_port not in captures:
            raise RuntimeError(f"No existe el puerto '{self.capture_port}'. "
                               f"Disponibles: {', '.join(captures) or 'ninguno'}")
        self.client.connect(self.capture_port, self.inport)

        self.router = OutputRouter(self.client, self.capture_port)
        log.info("MODEP %s", "detectado: la guitarra sale por el pedalboard"
                 if self.router.modep else "no activo: la guitarra sale limpia")

        settings = self.store.snapshot()
        engine_id, sound = self._choose(settings)
        self._launch(engine_id, sound)
        self._sync_choice(engine_id, sound, settings)

        self.running.set()
        self.worker = threading.Thread(target=self._worker, name="analysis", daemon=True)
        self.worker.start()

    def stop(self):
        self.running.clear()
        if self.client is None:
            return
        try:
            self._queue_midi([0xB0 | MIDI_CHANNEL, 123, 0])
            time.sleep(0.05)
            if self.router:
                self.router.restore()
        except jack.JackError:
            pass
        if self.backend:
            self.backend.stop()
        try:
            self.client.deactivate()
            self.client.close()
        except jack.JackError:
            pass
        self.client = None

    # --- callback de JACK (tiempo real) --------------------------------

    def _process(self, frames):
        try:
            self.audio_queue.put_nowait(self.inport.get_array().copy())
        except queue.Full:
            pass
        self.midi_out.clear_buffer()
        while self.midi_pending:
            try:
                message = self.midi_pending.popleft()
            except IndexError:
                break
            try:
                self.midi_out.write_midi_event(0, message)
            except jack.JackError:
                # Buffer MIDI lleno: se envía en el siguiente ciclo
                self.midi_pending.appendleft(message)
                break

    def _xrun(self, delay_us):
        self.xruns += 1
        self.worst_xrun_ms = max(self.worst_xrun_ms, delay_us / 1000)

    def _shutdown(self, status, reason):
        self.error = f"JACK se ha detenido: {reason}"
        self.jack_died.set()

    # --- MIDI ----------------------------------------------------------

    def _queue_midi(self, message):
        self.midi_pending.append(bytes(message))

    def _cc(self, number, value):
        self._queue_midi([0xB0 | MIDI_CHANNEL, number, int(value)])

    def _send_bend_range(self, semitones):
        # RPN 0: rango de pitch bend
        for number, value in ((101, 0), (100, 0), (6, semitones), (38, 0), (101, 127), (100, 127)):
            self._cc(number, value)

    def _send_bend(self, semitones, bend_range):
        value = int(round(8192 + semitones / bend_range * 8192))
        value = max(0, min(16383, value))
        self._queue_midi([0xE0 | MIDI_CHANNEL, value & 0x7F, value >> 7])

    def _send_sound_settings(self, s):
        if self.backend.gm:
            self._queue_midi([0xC0 | MIDI_CHANNEL, s["program"]])
        self._cc(7, s["volume"])
        self._cc(91, s["reverb"])
        self._cc(93, s["chorus"])
        self._send_bend_range(s["bend_range"])
        self._send_bend(0, s["bend_range"])
        self.backend.set_reverb_room(s["room"])

    # --- motores de síntesis -------------------------------------------

    def library_info(self):
        return {
            "dirs": self.sound_dirs,
            "engines": [{
                "id": cls.id, "label": cls.label, "kind": cls.kind, "gm": cls.gm,
                "setting": cls.setting, "available": cls.available(),
                "hint": cls.install_hint, "sounds": self.library[cls.kind],
            } for cls in BACKENDS.values()],
        }

    def rescan(self):
        self.library = scan_library(self.sound_dirs)

    def _announce(self, text, error=False):
        self.notice = {"id": self.notice["id"] + 1, "text": text, "error": error}
        (log.warning if error else log.info)("%s", text)

    def _default_sound(self, cls):
        sounds = self.library[cls.kind]
        if cls.id == "fluidsynth":
            for preferred in ("generaluser", "fluidr3_gm"):
                for sound in sounds:
                    if preferred in sound["id"].lower():
                        return sound["id"]
        return sounds[0]["id"] if sounds else None

    def _choose(self, settings):
        """(motor, archivo) que corresponden a los ajustes, con alternativas
        si el motor no está instalado o el archivo ya no existe."""
        order = [settings["engine"]] + [e for e in BACKENDS if e != settings["engine"]]
        for engine_id in order:
            cls = BACKENDS[engine_id]
            if not cls.available():
                continue
            ids = {s["id"] for s in self.library[cls.kind]}
            wanted = settings.get(cls.setting)
            sound = wanted if wanted in ids else self._default_sound(cls)
            if sound:
                return engine_id, sound
        raise RuntimeError(
            "No hay ningún motor de sonido utilizable: instala fluidsynth y deja al menos "
            "una SoundFont (.sf2) en /usr/share/sounds/sf2 o en app/sounds")

    def _launch(self, engine_id, sound):
        """Arranca un motor, lo conecta y reaplica el sonido. Sin bloqueos externos."""
        backend = BACKENDS[engine_id](self.synth_gain, self.effects)
        midi_in, outputs = backend.start(self.client, self.samplerate, sound)
        self.client.connect(self.midi_out, midi_in)
        self.backend = backend
        self.router.synth_ports = outputs
        settings = self.store.snapshot()
        self.router.apply(settings["output"])
        self._send_sound_settings(settings)
        log.info("%s listo con %s (MIDI %s, audio %s)", backend.label,
                 sound, midi_in, ", ".join(outputs))

    def _sync_choice(self, engine_id, sound, expected):
        """
        Deja en los ajustes lo que realmente está sonando, pero sólo donde
        nadie ha cambiado nada desde que empezó el cambio (`expected`): si el
        usuario ya pidió otra cosa, esa petición manda.
        """
        current = self.store.snapshot()
        wanted = {"engine": engine_id, BACKENDS[engine_id].setting: sound}
        changes = {k: v for k, v in wanted.items() if current[k] == expected[k]}
        if self.store.update(changes):
            self.settings_rev += 1

    def request_switch(self):
        """Cambia de motor o de archivo si los ajustes lo piden (en segundo plano)."""
        threading.Thread(target=self._switch, name="switch", daemon=True).start()

    def _switch(self):
        with self.switch_lock:
            if not self.running.is_set():
                return
            settings = self.store.snapshot()
            try:
                engine_id, sound = self._choose(settings)
            except RuntimeError as exc:
                self._announce(str(exc), True)
                return
            requested = settings["engine"]
            if engine_id != requested:
                self._announce(f"{BACKENDS[requested].label} no está disponible (falta el programa "
                               f"o no hay archivos .{BACKENDS[requested].kind}); se usa "
                               f"{BACKENDS[engine_id].label}", True)
            current = self.backend
            if current and current.id == engine_id and current.sound == sound and current.running:
                self._sync_choice(engine_id, sound, settings)
                return

            previous = (current.id, current.sound) if current else None
            self.loading = True
            try:
                self._reset_request = True      # el hilo de análisis apaga sus notas
                self._cc(123, 0)
                time.sleep(0.05)
                if current:
                    current.stop()
                    self.backend = None
                self._announce_loading(engine_id, sound)
                try:
                    self._launch(engine_id, sound)
                except (RuntimeError, jack.JackError) as exc:
                    self._announce(f"No se pudo cargar: {exc}", True)
                    if previous is None:
                        raise
                    self._launch(*previous)         # volver a lo que sonaba antes
                    engine_id, sound = previous
                    self._sync_choice(engine_id, sound, settings)
                    return
                self._sync_choice(engine_id, sound, settings)
                self._announce(f"{BACKENDS[engine_id].label}: "
                               f"{os.path.splitext(os.path.basename(sound))[0]}")
            except (RuntimeError, jack.JackError) as exc:
                self.error = f"Sin motor de sonido: {exc}"
                self._announce(self.error, True)
            finally:
                self.loading = False

    def _announce_loading(self, engine_id, sound):
        log.info("Cargando %s (%s)…", BACKENDS[engine_id].label, os.path.basename(sound))

    # --- cambios desde la interfaz -------------------------------------

    def apply(self, changes):
        """Aplica cambios de ajustes. Devuelve los que realmente cambiaron."""
        applied = self.store.update(changes)
        if not applied:
            return applied
        s = self.store.snapshot()

        if applied.keys() & {"engine", "sf2", "sfz"}:
            self.request_switch()
        if "program" in applied and self.backend and self.backend.gm:
            self._queue_midi([0xC0 | MIDI_CHANNEL, s["program"]])
        if "volume" in applied:
            self._cc(7, s["volume"])
        if "reverb" in applied:
            self._cc(91, s["reverb"])
        if "chorus" in applied:
            self._cc(93, s["chorus"])
        if "room" in applied and self.backend:
            self.backend.set_reverb_room(s["room"])
        if "bend_range" in applied:
            self._send_bend_range(s["bend_range"])
        if "bend_enabled" in applied and not s["bend_enabled"]:
            self._send_bend(0, s["bend_range"])

        if applied.keys() & {"transpose", "output", "mode"}:
            self._reset_request = True
        if "output" in applied and self.router:
            self.router.apply(s["output"])
        self._settings_request = True
        return applied

    def panic(self):
        self._reset_request = True
        self._cc(123, 0)    # all notes off
        self._cc(120, 0)    # all sound off

    def refresh_routing(self):
        """Reaplica el ruteo si MODEP se ha activado o desactivado."""
        if self.router and self.router.detect_modep() != self.router.modep:
            self.router.apply(self.store.snapshot()["output"])

    # --- hilo de análisis ----------------------------------------------

    def _update_tracker(self, settings):
        wanted = PolyTracker if settings["mode"] == "poly" else MonoTracker
        if isinstance(self.tracker, wanted):
            self.tracker.set_params(settings)
        else:
            self._all_notes_off()
            self.tracker = wanted(self.decimator.samplerate, settings)

    def _all_notes_off(self):
        for out in self.sounding.values():
            self._queue_midi([0x80 | MIDI_CHANNEL, out, 0])
        self.sounding.clear()

    def _worker(self):
        settings = self.store.snapshot()
        while self.running.is_set():
            try:
                blocks = [self.audio_queue.get(timeout=0.2)]
            except queue.Empty:
                continue
            # Vaciar la cola para no acumular retraso
            while True:
                try:
                    blocks.append(self.audio_queue.get_nowait())
                except queue.Empty:
                    break

            if self._settings_request:
                # Bajar la bandera antes de leer: un cambio posterior la vuelve a subir
                self._settings_request = False
                settings = self.store.snapshot()
                self._update_tracker(settings)
            if self._reset_request:
                self._reset_request = False
                self._all_notes_off()
                self.tracker.reset()

            samples = self.decimator.process(np.concatenate(blocks))
            events = self.tracker.process(samples)
            if events:
                self._handle_events(events, settings)
            self.live = dict(self.tracker.display, mode=settings["mode"],
                             synth=[note_name(n) for n in sorted(self.sounding.values())])

    def _handle_events(self, events, s):
        if self.loading or self.backend is None:
            return      # el motor se está cambiando: no hay a quién enviar notas
        synth_on = s["output"] in ("synth", "both")
        for kind, value in events:
            if kind == "on":
                if value in self.sounding:
                    self._queue_midi([0x80 | MIDI_CHANNEL, self.sounding.pop(value), 0])
                out = value + s["transpose"]
                if synth_on and 0 <= out <= 127:
                    self._queue_midi([0x90 | MIDI_CHANNEL, out, s["velocity"]])
                    self.sounding[value] = out
            elif kind == "off":
                out = self.sounding.pop(value, None)
                if out is not None:
                    self._queue_midi([0x80 | MIDI_CHANNEL, out, 0])
            elif kind == "bend" and s["bend_enabled"]:
                self._send_bend(value, s["bend_range"])

    # --- estado para la interfaz ---------------------------------------

    def status(self):
        return {
            "samplerate": getattr(self, "samplerate", None),
            "capture_port": self.capture_port,
            "modep": bool(self.router and self.router.modep),
            "engine": self.backend.id if self.backend else None,
            "engine_label": self.backend.label if self.backend else None,
            "sound": os.path.basename(self.backend.sound) if self.backend and self.backend.sound else None,
            "synth_running": bool(self.backend and self.backend.running),
            "loading": self.loading,
            "error": self.error,
            "xruns": self.xruns,
            "worst_xrun_ms": round(self.worst_xrun_ms, 1),
            "cpu_load": self._cpu_load(),
            "buffer": getattr(self, "blocksize", None),
            "effects": self.effects,
        }

    def _cpu_load(self):
        try:
            return round(self.client.cpu_load(), 1) if self.client else None
        except jack.JackError:
            return None

    def live_state(self):
        return dict(self.live, loading=self.loading)
