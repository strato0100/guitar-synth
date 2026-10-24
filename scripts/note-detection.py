#!/usr/bin/env python3
"""
Detector de notas con interfaz gráfica para guitarra eléctrica.

Lee audio desde JACK (Pisound en Patchbox OS), detecta la frecuencia
fundamental con aubio y muestra la nota, la frecuencia, la desviación
en cents y la cuerda al aire correspondiente (afinación estándar).

Uso:
    python3 note-detection.py
    python3 note-detection.py --fullscreen
    python3 note-detection.py --port system:capture_2 --method schmitt

Teclas:
    F11 / f   alternar pantalla completa
    Esc       salir de pantalla completa
    q         salir
"""

import argparse
import math
import queue
import statistics
import sys
import threading
import time
import tkinter as tk
from collections import deque

import aubio
import jack
import numpy as np

WINDOW_SIZE = 4096
HOP_SIZE = 512

MIN_FREQ = 60.0     # algo por debajo del Mi grave (82.41 Hz)
MAX_FREQ = 1400.0   # traste 24 de la 1ª cuerda ~1318 Hz

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F",
              "F#", "G", "G#", "A", "A#", "B"]

# Afinación estándar, de la 6ª a la 1ª cuerda: (etiqueta, número MIDI)
STANDARD_TUNING = [
    ("6 · E2", 40),
    ("5 · A2", 45),
    ("4 · D3", 50),
    ("3 · G3", 55),
    ("2 · B3", 59),
    ("1 · E4", 64),
]

# Colores
BG = "#111418"
FG = "#e8e8e8"
DIM = "#4a4f57"
GREEN = "#3ddc84"
YELLOW = "#f5c542"
RED = "#ff5c5c"
PANEL = "#1c2128"


def freq_to_midi(freq):
    return 69 + 12 * math.log2(freq / 440.0)


def midi_to_freq(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def midi_to_name(midi):
    return NOTE_NAMES[midi % 12], midi // 12 - 1


def cents_color(cents):
    if abs(cents) <= 5:
        return GREEN
    if abs(cents) <= 15:
        return YELLOW
    return RED


class PitchEngine:
    """Captura audio de JACK y detecta el pitch en un hilo aparte."""

    def __init__(self, port, method, confidence, gate, smoothing):
        self.port_name = port
        self.method = method
        self.min_confidence = confidence
        self.gate = gate

        self.audio_queue = queue.Queue(maxsize=256)
        self.history = deque(maxlen=max(1, smoothing))
        self.lock = threading.Lock()
        self.running = threading.Event()
        self.jack_died = threading.Event()

        # Último resultado válido: (frecuencia, timestamp)
        self.pitch = None
        # Nivel de entrada actual (RMS), útil para comprobar la señal
        self.level = 0.0

        self.client = jack.Client("NoteDetection")
        self.input_port = self.client.inports.register("input")
        self.samplerate = self.client.samplerate

        self.detector = aubio.pitch(method, WINDOW_SIZE, HOP_SIZE,
                                    self.samplerate)
        self.detector.set_unit("Hz")
        self.detector.set_silence(-60)

        self.client.set_process_callback(self._process)
        self.client.set_shutdown_callback(self._shutdown)
        self.worker = threading.Thread(target=self._worker, daemon=True)

    def _process(self, frames):
        try:
            self.audio_queue.put_nowait(self.input_port.get_array().copy())
        except queue.Full:
            pass

    def _shutdown(self, status, reason):
        self.jack_died.set()

    def start(self):
        self.client.activate()

        available = [p.name for p in self.client.get_ports(
            is_audio=True, is_output=True, is_physical=True)]
        if self.port_name not in available:
            self.client.deactivate()
            self.client.close()
            raise RuntimeError(
                f"No existe el puerto '{self.port_name}'. "
                f"Puertos de captura disponibles: {', '.join(available) or 'ninguno'}"
            )

        self.client.connect(self.port_name, self.input_port)
        self.running.set()
        self.worker.start()

    def stop(self):
        self.running.clear()
        try:
            self.client.deactivate()
            self.client.close()
        except jack.JackError:
            pass

    def _worker(self):
        pending = np.empty(0, dtype=np.float32)
        misses = 0

        while self.running.is_set():
            try:
                block = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            pending = np.concatenate((pending, block))

            while len(pending) >= HOP_SIZE:
                frame = pending[:HOP_SIZE]
                pending = pending[HOP_SIZE:]

                rms = float(np.sqrt(np.mean(frame * frame)))
                frequency = float(self.detector(frame)[0])
                confidence = float(self.detector.get_confidence())

                # schmitt no proporciona una confianza útil
                confident = (self.method == "schmitt"
                             or confidence >= self.min_confidence)
                valid = (rms > self.gate
                         and MIN_FREQ <= frequency <= MAX_FREQ
                         and confident)

                with self.lock:
                    self.level = rms

                if not valid:
                    misses += 1
                    # Tras varias lecturas malas seguidas, la nota terminó
                    if misses > 4:
                        self.history.clear()
                    continue

                misses = 0
                self.history.append(frequency)
                # Esperar unas cuantas lecturas para evitar saltos al atacar
                if len(self.history) < min(3, self.history.maxlen):
                    continue

                smoothed = statistics.median(self.history)
                with self.lock:
                    self.pitch = (smoothed, time.monotonic())

    def read(self):
        with self.lock:
            return self.pitch, self.level


class NoteDisplay:
    """Interfaz Tkinter que muestra la nota detectada."""

    METER_W = 640
    METER_H = 110
    HOLD_SECONDS = 1.0    # tras este tiempo sin señal la nota se atenúa
    CLEAR_SECONDS = 4.0   # tras este tiempo se borra

    def __init__(self, root, engine, port_label):
        self.root = root
        self.engine = engine

        root.title("Detector de notas - Guitarra")
        root.configure(bg=BG)
        root.geometry("800x480")
        root.minsize(640, 420)

        self.note_var = tk.StringVar(value="—")
        self.octave_var = tk.StringVar(value="")
        self.freq_var = tk.StringVar(value="Toca una nota…")
        self.cents_var = tk.StringVar(value="")

        note_row = tk.Frame(root, bg=BG)
        note_row.pack(pady=(10, 0))
        self.note_label = tk.Label(note_row, textvariable=self.note_var,
                                   font=("DejaVu Sans", 120, "bold"),
                                   fg=FG, bg=BG)
        self.note_label.pack(side=tk.LEFT)
        self.octave_label = tk.Label(note_row, textvariable=self.octave_var,
                                     font=("DejaVu Sans", 40),
                                     fg=DIM, bg=BG)
        self.octave_label.pack(side=tk.LEFT, anchor=tk.S, pady=(0, 30))

        tk.Label(root, textvariable=self.freq_var,
                 font=("DejaVu Sans Mono", 22), fg=FG, bg=BG).pack()

        self.meter = tk.Canvas(root, width=self.METER_W, height=self.METER_H,
                               bg=PANEL, highlightthickness=0)
        self.meter.pack(pady=8)
        self._draw_meter_scale()
        self.needle = self.meter.create_line(0, 0, 0, 0, width=4, fill=DIM)
        self.cents_text = self.meter.create_text(
            self.METER_W / 2, self.METER_H - 14, text="",
            font=("DejaVu Sans Mono", 14), fill=FG)

        strings_row = tk.Frame(root, bg=BG)
        strings_row.pack(pady=4)
        self.string_labels = []
        for label, _ in STANDARD_TUNING:
            w = tk.Label(strings_row, text=label, width=7,
                         font=("DejaVu Sans", 14, "bold"),
                         fg=DIM, bg=PANEL, padx=4, pady=6)
            w.pack(side=tk.LEFT, padx=4)
            self.string_labels.append(w)

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill=tk.X, side=tk.BOTTOM, padx=12, pady=8)
        tk.Label(bottom, text=f"{port_label} @ {engine.samplerate} Hz",
                 font=("DejaVu Sans", 10), fg=DIM, bg=BG).pack(side=tk.LEFT)
        self.level_bar = tk.Canvas(bottom, width=200, height=10,
                                   bg=PANEL, highlightthickness=0)
        self.level_bar.pack(side=tk.RIGHT)
        self.level_fill = self.level_bar.create_rectangle(0, 0, 0, 10,
                                                          fill=GREEN, width=0)
        tk.Label(bottom, text="Entrada", font=("DejaVu Sans", 10),
                 fg=DIM, bg=BG).pack(side=tk.RIGHT, padx=6)

        root.bind("<F11>", self._toggle_fullscreen)
        root.bind("f", self._toggle_fullscreen)
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.bind("q", lambda e: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._refresh()

    def _cents_to_x(self, cents):
        margin = 30
        span = self.METER_W - 2 * margin
        return margin + (cents + 50) / 100 * span

    def _draw_meter_scale(self):
        for cents in range(-50, 51, 10):
            x = self._cents_to_x(cents)
            tall = cents in (-50, 0, 50)
            self.meter.create_line(x, 10, x, 34 if tall else 24,
                                   fill=FG if cents == 0 else DIM, width=2)
            if tall:
                self.meter.create_text(x, 46, text=f"{cents:+d}" if cents else "0",
                                       font=("DejaVu Sans", 10), fill=DIM)
        # Zona "afinado" (±5 cents)
        self.meter.create_rectangle(self._cents_to_x(-5), 10,
                                    self._cents_to_x(5), 34,
                                    outline=GREEN, width=1)

    def _toggle_fullscreen(self, event=None):
        current = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not current)

    def _show_pitch(self, freq, active):
        midi_float = freq_to_midi(freq)
        midi = round(midi_float)
        cents = (midi_float - midi) * 100
        name, octave = midi_to_name(midi)
        color = cents_color(cents) if active else DIM

        self.note_var.set(name)
        self.octave_var.set(str(octave))
        self.note_label.configure(fg=FG if active else DIM)
        self.freq_var.set(f"{freq:7.2f} Hz  (objetivo {midi_to_freq(midi):.2f})")

        x = self._cents_to_x(max(-50, min(50, cents)))
        self.meter.coords(self.needle, x, 6, x, 60)
        self.meter.itemconfigure(self.needle, fill=color)
        self.meter.itemconfigure(self.cents_text, text=f"{cents:+5.1f} cents",
                                 fill=color)

        for widget, (_, string_midi) in zip(self.string_labels, STANDARD_TUNING):
            if active and string_midi == midi:
                widget.configure(fg=BG, bg=color)
            else:
                widget.configure(fg=DIM, bg=PANEL)

    def _clear(self):
        self.note_var.set("—")
        self.octave_var.set("")
        self.note_label.configure(fg=DIM)
        self.freq_var.set("Toca una nota…")
        self.meter.coords(self.needle, 0, 0, 0, 0)
        self.meter.itemconfigure(self.cents_text, text="")
        for widget in self.string_labels:
            widget.configure(fg=DIM, bg=PANEL)

    def _refresh(self):
        if self.engine.jack_died.is_set():
            self.freq_var.set("JACK se ha detenido")
            self.note_label.configure(fg=RED)
            return

        pitch, level = self.engine.read()
        if pitch is None:
            self._clear()
        else:
            freq, stamp = pitch
            age = time.monotonic() - stamp
            if age < self.HOLD_SECONDS:
                self._show_pitch(freq, active=True)
            elif age < self.CLEAR_SECONDS:
                self._show_pitch(freq, active=False)
            else:
                self._clear()

        # Medidor de entrada en dB (-60 dB .. 0 dB)
        db = 20 * math.log10(level) if level > 1e-6 else -120
        fraction = max(0.0, min(1.0, (db + 60) / 60))
        fill = RED if db > -3 else (YELLOW if db > -12 else GREEN)
        self.level_bar.coords(self.level_fill, 0, 0, 200 * fraction, 10)
        self.level_bar.itemconfigure(self.level_fill, fill=fill)

        self.root.after(30, self._refresh)

    def close(self):
        self.engine.stop()
        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Muestra en pantalla la nota que tocas en la guitarra.")
    parser.add_argument("--port", default="system:capture_1",
                        help="puerto de captura JACK (por defecto system:capture_1)")
    parser.add_argument("--method", default="yinfft",
                        choices=["yinfft", "yin", "yinfast", "schmitt", "mcomb", "fcomb"],
                        help="algoritmo de aubio (por defecto yinfft)")
    parser.add_argument("--confidence", type=float, default=0.7,
                        help="confianza mínima 0..1 para aceptar una lectura (no aplica a schmitt)")
    parser.add_argument("--gate", type=float, default=0.0005,
                        help="RMS mínimo para considerar que hay señal")
    parser.add_argument("--smoothing", type=int, default=5,
                        help="nº de lecturas para la mediana (más = más estable, más lento)")
    parser.add_argument("--fullscreen", action="store_true",
                        help="arrancar en pantalla completa")
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.exit(f"No se pudo abrir la interfaz gráfica ({exc}).\n"
                 "¿Hay escritorio/pantalla? Prueba con DISPLAY=:0 o por VNC.")

    try:
        engine = PitchEngine(args.port, args.method, args.confidence,
                             args.gate, args.smoothing)
        engine.start()
    except (jack.JackError, RuntimeError) as exc:
        root.destroy()
        sys.exit(f"Error de audio: {exc}")

    if args.fullscreen:
        root.attributes("-fullscreen", True)

    NoteDisplay(root, engine, args.port)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
