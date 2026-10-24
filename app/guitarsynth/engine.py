"""
Motor de la app: une JACK, el detector de notas y FluidSynth.

  system:capture_1 ──> guitar-synth:input ──> (hilo de análisis) ──┐
                                                                   │ MIDI
  guitar-synth:midi_out <──────────────────────────────────────────┘
          │
          └──> gsynth-fluid (FluidSynth) ──> system:playback_1/2

El callback de JACK sólo copia audio y escribe MIDI pendiente; todo el
análisis va en un hilo aparte. Toda la lógica de notas (qué suena, qué
hay que apagar) vive en ese hilo, así que el resto de la app sólo pide
cambios mediante banderas.
"""

import logging
import queue
import sys
import threading
import time
from collections import deque

import jack
import numpy as np

from .dsp import Decimator, note_name
from .fluid import DEFAULT_SOUNDFONT, FluidSynth
from .mono import MonoTracker
from .poly import PolyTracker
from .router import OutputRouter

log = logging.getLogger("engine")

MIDI_CHANNEL = 0


class Engine:
    def __init__(self, store, capture_port="system:capture_1", soundfont=None,
                 client_name="guitar-synth", fluid_gain=0.7):
        self.store = store
        self.capture_port = capture_port
        self.client_name = client_name
        self.fluid = FluidSynth(soundfont or DEFAULT_SOUNDFONT, gain=fluid_gain)

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
        self.decimator = Decimator(self.samplerate)

        self.client.set_process_callback(self._process)
        self.client.set_shutdown_callback(self._shutdown)
        self.client.activate()

        captures = [p.name for p in self.client.get_ports(is_audio=True, is_output=True,
                                                          is_physical=True)]
        if self.capture_port not in captures:
            raise RuntimeError(f"No existe el puerto '{self.capture_port}'. "
                               f"Disponibles: {', '.join(captures) or 'ninguno'}")
        self.client.connect(self.capture_port, self.inport)

        midi_in, synth_outputs = self.fluid.start(self.client, self.samplerate)
        self.client.connect(self.midi_out, midi_in)
        log.info("FluidSynth listo: MIDI %s, audio %s", midi_in, ", ".join(synth_outputs))

        self.router = OutputRouter(self.client, self.capture_port)
        self.router.synth_ports = synth_outputs
        log.info("MODEP %s", "detectado: la guitarra sale por el pedalboard"
                 if self.router.modep else "no activo: la guitarra sale limpia")

        settings = self.store.snapshot()
        self.router.apply(settings["output"])
        self._send_sound_settings(settings)

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
        self.fluid.stop()
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
        self._queue_midi([0xC0 | MIDI_CHANNEL, s["program"]])
        self._cc(7, s["volume"])
        self._cc(91, s["reverb"])
        self._cc(93, s["chorus"])
        self._send_bend_range(s["bend_range"])
        self._send_bend(0, s["bend_range"])
        self.fluid.set_reverb_room(s["room"])
        self.fluid.command("chorus on")

    # --- cambios desde la interfaz -------------------------------------

    def apply(self, changes):
        """Aplica cambios de ajustes. Devuelve los que realmente cambiaron."""
        applied = self.store.update(changes)
        if not applied:
            return applied
        s = self.store.snapshot()

        if "program" in applied:
            self._queue_midi([0xC0 | MIDI_CHANNEL, s["program"]])
        if "volume" in applied:
            self._cc(7, s["volume"])
        if "reverb" in applied:
            self._cc(91, s["reverb"])
        if "chorus" in applied:
            self._cc(93, s["chorus"])
        if "room" in applied:
            self.fluid.set_reverb_room(s["room"])
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
            "fluidsynth": self.fluid.running,
            "soundfont": self.fluid.soundfont,
            "error": self.error,
        }

    def live_state(self):
        return dict(self.live)
