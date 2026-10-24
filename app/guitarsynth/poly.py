"""
Seguimiento de notas polifónico (acordes).

Cada ~21 ms se calcula una FFT de ventana larga y se estiman las
fundamentales presentes: se elige la nota cuyos armónicos acumulan más
energía, se restan sus armónicos (respetando la suavidad espectral para
no borrar notas que coinciden con un armónico) y se repite.

Para que el resultado sea estable, cada nota tiene histéresis: se
enciende cuando aparece en `stability` análisis seguidos y se
apaga cuando lleva varios análisis seguidos sin aparecer. Un ataque de
púa limpia el historial para que el cambio de acorde sea rápido.

Las notas de un acorde no se emiten una a una: la primera nota que se
estabiliza abre una breve ventana (`poly_hold` ms) y todas las que se
estabilicen dentro de ella se emiten juntas, para que el acorde suene a la vez.

Eventos generados: ("on", midi), ("off", midi).
"""

import math
from collections import deque

import numpy as np

from .chords import identify_chord
from .dsp import (FMAX, FMIN, HOP, OnsetDetector, db, freq_to_midi, next_pow2,
                  note_name, rms)

ANALYSIS_EVERY = 4          # un análisis cada 4 pasos (~21 ms a 48 kHz)
MAX_PARTIAL = 5000.0        # armónicos más altos que se tienen en cuenta
MAX_HARMONICS = 16
HARMONIC_TOL_CENTS = 35
COMPRESSION = 0.5           # comprime amplitudes para que las notas débiles cuenten
PEAK_DB = 40.0              # ignorar picos 40 dB por debajo del más fuerte
HISTORY = 6                 # análisis recordados por nota
OFF_MISSES = 3              # análisis seguidos sin la nota para apagarla
EXPLAINED = 0.5             # fundamental ya explicada por armónicos de otra nota
OVERTONE_PENALTY = 2.0      # evidencia extra para notas que coinciden con un armónico
SKIP_AFTER_ONSET = 4        # análisis a esperar tras un ataque (rasgueo + ventana)
RELEASE_HOPS = 6
GATE_HYSTERESIS_DB = 6.0

# Duración de la ventana de análisis
WINDOWS = {"fast": 0.17, "precise": 0.34}


class PolyTracker:
    def __init__(self, samplerate, params):
        self.samplerate = samplerate
        self.fft_size = next_pow2(samplerate * WINDOWS["precise"])
        self.buffer = np.zeros(self.fft_size, dtype=np.float32)
        self.freqs_axis = np.fft.rfftfreq(self.fft_size, 1.0 / samplerate)
        self.bin_hz = samplerate / self.fft_size
        self._windows = {}
        self._pending = np.empty(0, dtype=np.float32)
        self.onsets = OnsetDetector(HOP / samplerate)
        self.set_params(params)
        self.reset()

    def set_params(self, params):
        self.gate_db = float(params["gate_db"])
        self.onset_db = float(params["onset_db"])
        self.stability = int(params["stability"])
        self.salience = float(params["salience"])
        self.window_name = params["poly_window"]
        self.a4 = float(params["a4"])
        hop_seconds = HOP / self.samplerate
        self.hold_hops = round(float(params["poly_hold"]) / 1000 / hop_seconds)

    def reset(self):
        self.ready = set()          # notas estables a la espera de salir juntas
        self.flush_in = 0
        self.active = set()
        self.history = {}
        self.hop_count = 0
        self.skip = 0
        self.release_count = 0
        self.onsets.reset()
        self.display = {"notes": [], "chord": None, "level_db": -180.0}

    def _window(self, length):
        if length not in self._windows:
            self._windows[length] = np.hanning(length).astype(np.float32)
        return self._windows[length]

    # ------------------------------------------------------------------

    def process(self, samples):
        events = []
        if len(self._pending):
            samples = np.concatenate((self._pending, samples))
        offset = 0
        while len(samples) - offset >= HOP:
            self.buffer[:-HOP] = self.buffer[HOP:]
            self.buffer[-HOP:] = samples[offset:offset + HOP]
            offset += HOP
            self._step(events)
        self._pending = samples[offset:].copy()
        return events

    def _step(self, events):
        x = self.buffer
        level_db = db(rms(x[-1024:]))
        if self.onsets.update(db(rms(x[-512:])), self.onset_db, self.gate_db):
            # Ataque nuevo: olvidar el historial para cambiar rápido de acorde
            self.history = {n: deque(maxlen=HISTORY) for n in self.active}
            self.skip = SKIP_AFTER_ONSET

        gate = self.gate_db - (GATE_HYSTERESIS_DB if self.active else 0.0)
        self.release_count = self.release_count + 1 if level_db < gate else 0
        if self.release_count >= RELEASE_HOPS and self.active:
            for n in sorted(self.active):
                events.append(("off", n))
            self.active.clear()
            self.history.clear()
            self.ready.clear()

        if self.flush_in > 0:
            self.flush_in -= 1
            if self.flush_in == 0:
                self._flush_ready(events)

        self.hop_count += 1
        if self.hop_count % ANALYSIS_EVERY == 0:
            if self.skip > 0:
                self.skip -= 1
            elif level_db > gate:
                self._update_notes(events, self._estimate())

        notes = sorted(self.active)
        self.display = {"notes": [note_name(n) for n in notes],
                        "midi": notes,
                        "chord": identify_chord(notes),
                        "level_db": level_db}

    def _update_notes(self, events, detected):
        for n in detected:
            self.history.setdefault(n, deque(maxlen=HISTORY))
        for n, hist in list(self.history.items()):
            hist.append(n in detected)

            if n not in self.active:
                recent = list(hist)[-self.stability:]
                if len(recent) == self.stability and all(recent):
                    if self.hold_hops <= 0:
                        self.active.add(n)
                        events.append(("on", n))
                    elif n not in self.ready:
                        if not self.ready:
                            self.flush_in = self.hold_hops
                        self.ready.add(n)
            else:
                recent = list(hist)[-OFF_MISSES:]
                if len(recent) == OFF_MISSES and not any(recent):
                    self.active.discard(n)
                    events.append(("off", n))

            if n not in self.active and not any(hist):
                self.ready.discard(n)
                del self.history[n]

    def _flush_ready(self, events):
        for n in sorted(self.ready):
            hist = self.history.get(n)
            if hist and hist[-1]:       # sigue sonando
                self.active.add(n)
                events.append(("on", n))
        self.ready.clear()

    # ------------------------------------------------------------------

    def _estimate(self):
        length = next_pow2(self.samplerate * WINDOWS.get(self.window_name, WINDOWS["fast"]))
        length = min(length, self.fft_size)
        frame = self.buffer[-length:] * self._window(length)
        # Relleno con ceros hasta fft_size: misma rejilla de frecuencias
        spectrum = np.abs(np.fft.rfft(frame, self.fft_size))
        freqs, amps = self._find_peaks(spectrum)
        return self._estimate_notes(freqs, amps)

    def _find_peaks(self, spectrum):
        """Picos espectrales con frecuencia y amplitud interpoladas."""
        mag_db = 20 * np.log10(spectrum + 1e-12)
        lo = max(1, int(FMIN / self.bin_hz))
        hi = min(len(spectrum) - 1, int(MAX_PARTIAL / self.bin_hz))

        center = mag_db[lo:hi]
        is_peak = (center > mag_db[lo - 1:hi - 1]) & (center >= mag_db[lo + 1:hi + 1])
        idx = np.nonzero(is_peak)[0] + lo
        if len(idx) == 0:
            return np.empty(0), np.empty(0)

        top = mag_db[idx].max()
        floor = np.median(center) + 10
        idx = idx[(mag_db[idx] > top - PEAK_DB) & (mag_db[idx] > floor)]
        if len(idx) == 0:
            return np.empty(0), np.empty(0)

        a, b, c = mag_db[idx - 1], mag_db[idx], mag_db[idx + 1]
        denom = a - 2 * b + c
        safe = np.where(denom != 0, denom, 1)
        offset = np.where(denom != 0, 0.5 * (a - c) / safe, 0)
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
            return set()

        original = amps ** COMPRESSION
        residual = original.copy()

        # Candidatos: cada pico en el rango de fundamentales, uno por nota
        candidates = {}
        for i, f in enumerate(freqs):
            if FMIN <= f <= FMAX:
                midi = round(freq_to_midi(f, self.a4))
                if midi not in candidates or amps[i] > amps[candidates[midi]]:
                    candidates[midi] = i
        harmonics = {m: self._harmonic_matches(freqs[i], freqs)
                     for m, i in candidates.items()}

        selected = set()
        overtones = set()   # picos que son armónico >= 3 de una nota ya elegida
        best_salience = None
        while candidates and len(selected) < 6:
            scores = {}
            for m, i in candidates.items():
                # Si la fundamental ya está explicada por otra nota, descartar
                if residual[i] < EXPLAINED * original[i]:
                    continue
                scores[m] = sum(residual[j] / h for h, j in harmonics[m].items())
            if not scores:
                break
            if best_salience is None:
                best_salience = max(scores.values())

            # La mejor candidata que supere su umbral. Las que coinciden con un
            # armónico de otra nota (p. ej. el D5 que genera un G3) necesitan
            # el doble de evidencia: son la fuente típica de "add9" falsos.
            midi = None
            for m in sorted(scores, key=scores.get, reverse=True):
                factor = OVERTONE_PENALTY if candidates[m] in overtones else 1.0
                if scores[m] > 0 and scores[m] >= self.salience * factor * best_salience:
                    midi = m
                    break
            if midi is None:
                break

            selected.add(midi)
            del candidates[midi]

            # Restar los armónicos de la nota elegida. Para h >= 2 sólo se resta
            # lo esperado según los armónicos vecinos (suavidad espectral).
            matches = harmonics[midi]
            before = residual.copy()
            for h, j in matches.items():
                if h == 1:
                    residual[j] = 0
                    continue
                if h >= 3:
                    overtones.add(j)
                neighbours = [before[matches[k]] for k in (h - 1, h + 1) if k in matches]
                expected = np.mean(neighbours) if neighbours else before[j]
                residual[j] = max(0.0, residual[j] - min(before[j], expected))

        return selected
