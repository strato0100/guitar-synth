#!/usr/bin/env python3
"""
Detector de acordes con interfaz gráfica para guitarra eléctrica.

Lee audio desde JACK (Pisound en Patchbox OS) y detecta varias notas
simultáneas mediante análisis espectral:

  1. FFT con ventana larga (~0.3 s) para separar notas graves cercanas.
  2. Búsqueda de picos en el espectro con interpolación parabólica.
  3. Estimación iterativa de fundamentales: se elige la nota cuyos
     armónicos acumulan más energía, se restan sus armónicos del
     espectro y se repite hasta que no quede energía significativa.
  4. Las notas estables se comparan con plantillas de acordes.

Uso:
    python3 chord-detection.py
    python3 chord-detection.py --fullscreen
    python3 chord-detection.py --debug          # imprime las notas en consola

Teclas:
    F11 / f   alternar pantalla completa
    Esc       salir de pantalla completa
    q         salir
"""

import argparse
import math
import queue
import sys
import threading
import time
import tkinter as tk
from collections import Counter, deque

import jack
import numpy as np

ANALYSIS_SECONDS = 0.25   # duración mínima de la ventana de análisis
HOPS_PER_WINDOW = 8       # análisis por ventana (solapamiento)

MIN_FUND = 75.0           # algo por debajo del Mi grave (82.41 Hz)
MAX_FUND = 1400.0         # traste 24 de la 1ª cuerda ~1318 Hz
MAX_PARTIAL = 5000.0      # armónicos más altos que se tienen en cuenta
MAX_HARMONICS = 10
HARMONIC_TOL_CENTS = 35   # tolerancia al buscar cada armónico
COMPRESSION = 0.5         # comprime amplitudes para que las notas débiles cuenten

STABLE_FRAMES = 5         # una nota debe aparecer en STABLE_MIN de los
STABLE_MIN = 3            # últimos STABLE_FRAMES análisis

KEYBOARD_LOW = 40         # E2, 6ª cuerda al aire
KEYBOARD_HIGH = 88        # E6, traste 24 de la 1ª cuerda

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F",
              "F#", "G", "G#", "A", "A#", "B"]
BLACK_KEYS = {1, 3, 6, 8, 10}

# (sufijo, intervalos desde la raíz). El orden marca la preferencia cuando
# un mismo conjunto de notas admite varios nombres.
CHORD_TYPES = [
    ("", (0, 4, 7)),
    ("m", (0, 3, 7)),
    ("5", (0, 7)),
    ("7", (0, 4, 7, 10)),
    ("m7", (0, 3, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("sus4", (0, 5, 7)),
    ("sus2", (0, 2, 7)),
    ("6", (0, 4, 7, 9)),
    ("m6", (0, 3, 7, 9)),
    ("add9", (0, 2, 4, 7)),
    ("madd9", (0, 2, 3, 7)),
    ("7sus4", (0, 5, 7, 10)),
    ("9", (0, 2, 4, 7, 10)),
    ("m9", (0, 2, 3, 7, 10)),
    ("maj9", (0, 2, 4, 7, 11)),
    ("dim", (0, 3, 6)),
    ("m7b5", (0, 3, 6, 10)),
    ("dim7", (0, 3, 6, 9)),
    ("aug", (0, 4, 8)),
    ("mMaj7", (0, 3, 7, 11)),
]


def _build_templates():
    templates = []
    for suffix, intervals in CHORD_TYPES:
        templates.append((suffix, frozenset(intervals)))
    # En la guitarra es habitual omitir la quinta en acordes de 4+ notas
    for suffix, intervals in CHORD_TYPES:
        if len(intervals) >= 4 and 7 in intervals:
            templates.append((suffix, frozenset(intervals) - {7}))
    return templates


CHORD_TEMPLATES = _build_templates()

# Colores
BG = "#111418"
FG = "#e8e8e8"
DIM = "#4a4f57"
GREEN = "#3ddc84"
YELLOW = "#f5c542"
RED = "#ff5c5c"
BLUE = "#4ea1ff"
PANEL = "#1c2128"


def freq_to_midi(freq):
    return 69 + 12 * math.log2(freq / 440.0)


def midi_to_label(midi):
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def next_pow2(n):
    return 1 << (int(n) - 1).bit_length()


def _match_chord(pcs, bass):
    """Busca una plantilla que coincida exactamente con las clases de nota."""
    matches = []
    for root in pcs:
        relative = frozenset((p - root) % 12 for p in pcs)
        for rank, (suffix, intervals) in enumerate(CHORD_TEMPLATES):
            if relative == intervals:
                # Preferir la raíz en el bajo, luego el orden de la tabla
                matches.append((root != bass, rank, root, suffix))
    if not matches:
        return None
    _, _, root, suffix = min(matches)
    name = NOTE_NAMES[root] + suffix
    if root != bass:
        name += "/" + NOTE_NAMES[bass]
    return name, root


def identify_chord(notes):
    """
    Devuelve (nombre, raíz, exacto) para una lista de notas MIDI,
    o None si no se reconoce ningún acorde.
    """
    if not notes:
        return None
    bass = min(notes) % 12
    pcs = frozenset(n % 12 for n in notes)

    if len(pcs) == 1:
        return NOTE_NAMES[bass], bass, True

    result = _match_chord(pcs, bass)
    if result:
        return result[0], result[1], True

    # Aproximación: probar quitando una nota (posible falso positivo)
    if len(pcs) >= 3:
        for pc in sorted(pcs - {bass}):
            result = _match_chord(pcs - {pc}, bass)
            if result:
                return result[0], result[1], False
    return None


class ChordEngine:
    """Captura audio de JACK y estima las notas simultáneas en un hilo aparte."""

    def __init__(self, port, gate, peak_db, rel_salience, max_notes, debug):
        self.port_name = port
        self.gate = gate
        self.peak_db = peak_db
        self.rel_salience = rel_salience
        self.max_notes = max_notes
        self.debug = debug

        self.audio_queue = queue.Queue(maxsize=512)
        self.frames = deque(maxlen=STABLE_FRAMES)
        self.lock = threading.Lock()
        self.running = threading.Event()
        self.jack_died = threading.Event()

        # Último resultado estable: (notas MIDI, timestamp)
        self.result = None
        self.level = 0.0

        self.client = jack.Client("ChordDetection")
        self.input_port = self.client.inports.register("input")
        self.samplerate = self.client.samplerate

        self.fft_size = next_pow2(self.samplerate * ANALYSIS_SECONDS)
        self.hop = self.fft_size // HOPS_PER_WINDOW
        self.bin_hz = self.samplerate / self.fft_size
        self.window = np.hanning(self.fft_size).astype(np.float32)

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
        buffer = np.zeros(self.fft_size, dtype=np.float32)
        new_samples = 0

        while self.running.is_set():
            try:
                blocks = [self.audio_queue.get(timeout=0.2)]
            except queue.Empty:
                continue
            # Vaciar la cola para no acumular retraso si el análisis tarda
            while True:
                try:
                    blocks.append(self.audio_queue.get_nowait())
                except queue.Empty:
                    break

            incoming = np.concatenate(blocks)[-self.fft_size:]
            n = len(incoming)
            buffer[:-n] = buffer[n:]
            buffer[-n:] = incoming
            new_samples += n

            if new_samples >= self.hop:
                new_samples = 0
                self._analyze(buffer)

    def _analyze(self, buffer):
        recent = buffer[-self.hop:]
        rms = float(np.sqrt(np.mean(recent * recent)))

        notes = []
        if rms > self.gate:
            spectrum = np.abs(np.fft.rfft(buffer * self.window))
            freqs, amps = self._find_peaks(spectrum)
            notes = self._estimate_notes(freqs, amps)

        self.frames.append(set(notes))
        counts = Counter(n for frame in self.frames for n in frame)
        stable = sorted(n for n, c in counts.items() if c >= STABLE_MIN)

        with self.lock:
            self.level = rms
            if stable:
                previous = self.result[0] if self.result else None
                self.result = (stable, time.monotonic())
                if self.debug and stable != previous:
                    chord = identify_chord(stable)
                    print(f"{' '.join(midi_to_label(n) for n in stable):30s} "
                          f"{chord[0] if chord else '?'}", flush=True)

    def _find_peaks(self, spectrum):
        """Picos espectrales con frecuencia y amplitud interpoladas."""
        mag_db = 20 * np.log10(spectrum + 1e-12)
        lo = max(1, int(MIN_FUND / self.bin_hz))
        hi = min(len(spectrum) - 1, int(MAX_PARTIAL / self.bin_hz))

        center = mag_db[lo:hi]
        is_peak = (center > mag_db[lo - 1:hi - 1]) & (center >= mag_db[lo + 1:hi + 1])
        idx = np.nonzero(is_peak)[0] + lo
        if len(idx) == 0:
            return np.empty(0), np.empty(0)

        top = mag_db[idx].max()
        floor = np.median(center) + 10
        idx = idx[(mag_db[idx] > top - self.peak_db) & (mag_db[idx] > floor)]
        if len(idx) == 0:
            return np.empty(0), np.empty(0)

        a, b, c = mag_db[idx - 1], mag_db[idx], mag_db[idx + 1]
        denom = a - 2 * b + c
        offset = np.where(denom != 0, 0.5 * (a - c) / np.where(denom != 0, denom, 1), 0)
        freqs = (idx + offset) * self.bin_hz
        amps_db = b - 0.25 * (a - c) * offset
        return freqs, 10 ** (amps_db / 20)

    @staticmethod
    def _harmonic_matches(f0, freqs):
        """Índice del pico que corresponde a cada armónico de f0."""
        matches = {}
        for h in range(1, MAX_HARMONICS + 1):
            target = h * f0
            if target > MAX_PARTIAL:
                break
            k = np.searchsorted(freqs, target)
            best = None
            for j in (k - 1, k):
                if 0 <= j < len(freqs):
                    cents = abs(1200 * math.log2(freqs[j] / target))
                    if cents <= HARMONIC_TOL_CENTS and (best is None or cents < best[0]):
                        best = (cents, j)
            if best:
                matches[h] = best[1]
        return matches

    def _estimate_notes(self, freqs, amps):
        if len(freqs) == 0:
            return []

        original = amps ** COMPRESSION
        residual = original.copy()

        # Candidatos: cada pico en el rango de fundamentales, uno por nota
        candidates = {}
        for i, f in enumerate(freqs):
            if MIN_FUND <= f <= MAX_FUND:
                midi = round(freq_to_midi(f))
                if midi not in candidates or amps[i] > amps[candidates[midi]]:
                    candidates[midi] = i
        harmonics = {m: self._harmonic_matches(freqs[i], freqs)
                     for m, i in candidates.items()}

        selected = []
        best_salience = None
        while candidates and len(selected) < self.max_notes:
            scores = {}
            for m, i in candidates.items():
                # Si la fundamental ya está explicada por otra nota, descartar
                if residual[i] < 0.2 * original[i]:
                    continue
                scores[m] = sum(residual[j] / h for h, j in harmonics[m].items())
            if not scores:
                break

            midi = max(scores, key=scores.get)
            salience = scores[midi]
            if best_salience is None:
                best_salience = salience
            if salience <= 0 or salience < self.rel_salience * best_salience:
                break

            selected.append(midi)
            del candidates[midi]

            # Restar los armónicos de la nota elegida. Para h >= 2 sólo se resta
            # lo esperado según los armónicos vecinos (suavidad espectral), así
            # una nota que coincide con un armónico de otra no desaparece.
            matches = harmonics[midi]
            before = residual.copy()
            for h, j in matches.items():
                if h == 1:
                    residual[j] = 0
                    continue
                neighbours = [before[matches[k]] for k in (h - 1, h + 1) if k in matches]
                expected = np.mean(neighbours) if neighbours else before[j]
                residual[j] = max(0.0, residual[j] - min(before[j], expected))

        return sorted(selected)

    def read(self):
        with self.lock:
            return self.result, self.level


class ChordDisplay:
    """Interfaz Tkinter que muestra el acorde y las notas detectadas."""

    KEYS_W = 760
    KEYS_H = 110
    HOLD_SECONDS = 1.5    # tras este tiempo sin señal el acorde se atenúa
    CLEAR_SECONDS = 5.0   # tras este tiempo se borra

    def __init__(self, root, engine, port_label):
        self.root = root
        self.engine = engine

        root.title("Detector de acordes - Guitarra")
        root.configure(bg=BG)
        root.geometry("800x480")
        root.minsize(780, 440)

        self.chord_var = tk.StringVar(value="—")
        self.notes_var = tk.StringVar(value="Toca un acorde…")

        self.chord_label = tk.Label(root, textvariable=self.chord_var,
                                    font=("DejaVu Sans", 90, "bold"),
                                    fg=DIM, bg=BG)
        self.chord_label.pack(pady=(6, 0))

        tk.Label(root, textvariable=self.notes_var,
                 font=("DejaVu Sans Mono", 18), fg=FG, bg=BG).pack()

        chips = tk.Frame(root, bg=BG)
        chips.pack(pady=8)
        self.pc_labels = []
        for name in NOTE_NAMES:
            w = tk.Label(chips, text=name, width=3,
                         font=("DejaVu Sans", 14, "bold"),
                         fg=DIM, bg=PANEL, pady=6)
            w.pack(side=tk.LEFT, padx=3)
            self.pc_labels.append(w)

        self.keyboard = tk.Canvas(root, width=self.KEYS_W, height=self.KEYS_H,
                                  bg=BG, highlightthickness=0)
        self.keyboard.pack(pady=4)
        self.keys = {}
        self._draw_keyboard()

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill=tk.X, side=tk.BOTTOM, padx=12, pady=8)
        tk.Label(bottom,
                 text=f"{port_label} @ {engine.samplerate} Hz · "
                      f"FFT {engine.fft_size} ({engine.bin_hz:.1f} Hz/bin)",
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

    def _draw_keyboard(self):
        whites = [m for m in range(KEYBOARD_LOW, KEYBOARD_HIGH + 1)
                  if m % 12 not in BLACK_KEYS]
        key_w = self.KEYS_W / len(whites)
        white_x = {}

        for n, midi in enumerate(whites):
            x = n * key_w
            white_x[midi] = x
            self.keys[midi] = (self.keyboard.create_rectangle(
                x, 0, x + key_w - 1, self.KEYS_H, fill="#d8d8d8", outline=BG), "#d8d8d8")
            if midi % 12 in (0, 4):   # rotular C y E como referencia
                self.keyboard.create_text(x + key_w / 2, self.KEYS_H - 10,
                                          text=midi_to_label(midi),
                                          font=("DejaVu Sans", 8), fill="#555")

        black_w = key_w * 0.6
        for midi in range(KEYBOARD_LOW, KEYBOARD_HIGH + 1):
            if midi % 12 in BLACK_KEYS:
                x = white_x[midi - 1] + key_w - black_w / 2
                self.keys[midi] = (self.keyboard.create_rectangle(
                    x, 0, x + black_w, self.KEYS_H * 0.62, fill="#222", outline=BG), "#222")

    def _toggle_fullscreen(self, event=None):
        current = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", not current)

    def _show(self, notes, active):
        chord = identify_chord(notes)
        pcs = {n % 12 for n in notes}

        if chord:
            name, root, exact = chord
            color = (FG if exact else YELLOW) if active else DIM
        else:
            name, root = "?", None
            color = RED if active else DIM

        self.chord_var.set(name)
        self.chord_label.configure(fg=color)
        self.notes_var.set(" ".join(midi_to_label(n) for n in notes))

        for pc, widget in enumerate(self.pc_labels):
            if pc in pcs:
                bg = (GREEN if pc == root else BLUE) if active else DIM
                widget.configure(fg=BG, bg=bg)
            else:
                widget.configure(fg=DIM, bg=PANEL)

        for midi, (item, base) in self.keys.items():
            if midi in notes:
                fill = (GREEN if midi % 12 == root else BLUE) if active else "#777"
            else:
                fill = base
            self.keyboard.itemconfigure(item, fill=fill)

    def _clear(self):
        self.chord_var.set("—")
        self.chord_label.configure(fg=DIM)
        self.notes_var.set("Toca un acorde…")
        for widget in self.pc_labels:
            widget.configure(fg=DIM, bg=PANEL)
        for item, base in self.keys.values():
            self.keyboard.itemconfigure(item, fill=base)

    def _refresh(self):
        if self.engine.jack_died.is_set():
            self.notes_var.set("JACK se ha detenido")
            self.chord_label.configure(fg=RED)
            return

        result, level = self.engine.read()
        if result is None:
            self._clear()
        else:
            notes, stamp = result
            age = time.monotonic() - stamp
            if age < self.HOLD_SECONDS:
                self._show(notes, active=True)
            elif age < self.CLEAR_SECONDS:
                self._show(notes, active=False)
            else:
                self._clear()

        # Medidor de entrada en dB (-60 dB .. 0 dB)
        db = 20 * math.log10(level) if level > 1e-6 else -120
        fraction = max(0.0, min(1.0, (db + 60) / 60))
        fill = RED if db > -3 else (YELLOW if db > -12 else GREEN)
        self.level_bar.coords(self.level_fill, 0, 0, 200 * fraction, 10)
        self.level_bar.itemconfigure(self.level_fill, fill=fill)

        self.root.after(40, self._refresh)

    def close(self):
        self.engine.stop()
        self.root.destroy()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Muestra en pantalla el acorde y las notas que tocas en la guitarra.")
    parser.add_argument("--port", default="system:capture_1",
                        help="puerto de captura JACK (por defecto system:capture_1)")
    parser.add_argument("--gate", type=float, default=0.0005,
                        help="RMS mínimo para considerar que hay señal")
    parser.add_argument("--peak-db", type=float, default=40.0,
                        help="ignorar picos más de N dB por debajo del más fuerte")
    parser.add_argument("--salience", type=float, default=0.25,
                        help="fuerza mínima de una nota respecto a la más fuerte (0..1); "
                             "súbelo si aparecen notas de más, bájalo si faltan")
    parser.add_argument("--max-notes", type=int, default=6,
                        help="máximo de notas simultáneas")
    parser.add_argument("--fullscreen", action="store_true",
                        help="arrancar en pantalla completa")
    parser.add_argument("--debug", action="store_true",
                        help="imprimir en consola las notas detectadas")
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.exit(f"No se pudo abrir la interfaz gráfica ({exc}).\n"
                 "¿Hay escritorio/pantalla? Prueba con DISPLAY=:0 o por VNC.")

    try:
        engine = ChordEngine(args.port, args.gate, args.peak_db,
                             args.salience, args.max_notes, args.debug)
        engine.start()
    except (jack.JackError, RuntimeError) as exc:
        root.destroy()
        sys.exit(f"Error de audio: {exc}")

    if args.fullscreen:
        root.attributes("-fullscreen", True)

    ChordDisplay(root, engine, args.port)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
