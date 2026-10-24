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
from .dsp import (FMAX, FMIN, OnsetDetector, db, freq_to_midi, hop_size, next_pow2,
                  note_name, rms)

ANALYSIS_EVERY = 4          # un análisis cada 4 pasos (~21 ms a 48 kHz)
MAX_PARTIAL = 5000.0        # armónicos más altos que se tienen en cuenta
MAX_HARMONICS = 16
HARMONIC_TOL_CENTS = 35
COMPRESSION = 0.5           # comprime amplitudes para que las notas débiles cuenten
PEAK_DB = 40.0              # ignorar picos 40 dB por debajo del más fuerte
HISTORY = 14                # análisis recordados por nota (más que el máximo de 'stability' + margen)
LATE_EXTRA = 2              # análisis extra para aceptar una nota nueva con el acorde ya sonando
ESTABLISHED_NOTES = 3       # con tantas notas activas se considera que el acorde ya está establecido
LATE_EVIDENCE = 0.25        # pico mínimo de una nota nueva con el acorde ya sonando
MIN_FUNDAMENTAL = 0.12      # una nota debe tener su propia fundamental como pico (no sólo armónicos)
MIN_FUNDAMENTAL_BASS = 0.05 # ... algo menos para los graves, donde la fundamental suele ser débil
BASS_LIMIT = 45             # A2: por debajo se aplica el umbral de graves
OFF_MISSES = 3              # análisis seguidos sin la nota para apagarla
EXPLAINED = 0.5             # fundamental ya explicada por armónicos de otra nota
OVERTONE_PENALTY = 2.0      # evidencia extra para notas que coinciden con un armónico
SKIP_AFTER_ONSET = 4        # análisis a esperar tras un ataque (rasgueo + ventana)
RELEASE_HOPS = 6
GATE_HYSTERESIS_DB = 6.0

# Duración de la ventana de análisis
WINDOWS = {"fast": 0.17, "precise": 0.34}

# Afinación estándar (número MIDI de las cuerdas al aire, de la 6ª a la 1ª)
TUNING = (40, 45, 50, 55, 59, 64)
MAX_FRET = 12
VOICING_MIN_PITCH_CLASSES = 3   # sólo se reconstruye la digitación si hay un acorde
VOICING_EVIDENCE = 0.22         # pico mínimo (relativo al mayor) para dar una nota por tocada
FRET_COST = 0.03                # preferencia por trastes bajos y cuerdas al aire
BASE_BONUS = 0.4                # prioridad de las notas que el estimador ya había confirmado
PEAK_TOL_CENTS = 40
CLUSTER_RATIO = 2.0             # una nota a un semitono de otra 2 veces más fuerte se descarta

NOISE_FACTOR = 1.3          # cuánto del espectro de ruido medido se resta (1 = exacto)


class PolyTracker:
    def __init__(self, samplerate, params):
        self.samplerate = samplerate
        self.hop = hop_size(samplerate)
        self.level_len = int(samplerate * 0.0213)
        self.onset_len = int(samplerate * 0.0107)
        self.fft_size = next_pow2(samplerate * WINDOWS["precise"])
        self.buffer = np.zeros(self.fft_size, dtype=np.float32)
        self.freqs_axis = np.fft.rfftfreq(self.fft_size, 1.0 / samplerate)
        self.bin_hz = samplerate / self.fft_size
        self._windows = {}
        self.noise = {}             # perfil de ruido por ventana: nombre -> magnitudes
        self._pending = np.empty(0, dtype=np.float32)
        self.onsets = OnsetDetector(self.hop / samplerate)
        self.set_params(params)
        self.reset()

    def set_params(self, params):
        self.gate_db = float(params["gate_db"])
        self.onset_db = float(params["onset_db"])
        self.stability = int(params["stability"])
        self.salience = float(params["salience"])
        self.window_name = params["poly_window"]
        self.a4 = float(params["a4"])
        hop_seconds = self.hop / self.samplerate
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

    def _frame_length(self, name):
        length = next_pow2(self.samplerate * WINDOWS.get(name, WINDOWS["fast"]))
        return min(length, self.fft_size)

    def calibrate_noise(self, samples):
        """
        Mide el espectro medio del ruido de fondo (zumbido, soplido) a partir de
        audio sin tocar, para restarlo después. Devuelve el nivel medido en dBFS.
        """
        profile = {}
        for name in WINDOWS:
            length = self._frame_length(name)
            window = self._window(length)
            step = max(1, length // 2)
            spectra = [np.abs(np.fft.rfft(samples[i:i + length] * window, self.fft_size))
                       for i in range(0, len(samples) - length + 1, step)]
            if spectra:
                profile[name] = np.mean(spectra, axis=0)
        self.noise = profile
        return db(rms(samples)) if len(samples) else -180.0

    def clear_noise(self):
        self.noise = {}

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
        hop = self.hop
        while len(samples) - offset >= hop:
            self.buffer[:-hop] = self.buffer[hop:]
            self.buffer[-hop:] = samples[offset:offset + hop]
            offset += hop
            self._step(events)
        self._pending = samples[offset:].copy()
        return events

    def _step(self, events):
        x = self.buffer
        level_db = db(rms(x[-self.level_len:]))
        if self.onsets.update(db(rms(x[-self.onset_len:])), self.onset_db, self.gate_db):
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
                # Con el acorde ya sonando, una nota nueva suele ser un pico espurio
                needed = self.stability + (LATE_EXTRA if len(self.active) >= ESTABLISHED_NOTES else 0)
                recent = list(hist)[-needed:]
                if len(recent) == needed and all(recent):
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
        length = self._frame_length(self.window_name)
        frame = self.buffer[-length:] * self._window(length)
        # Relleno con ceros hasta fft_size: misma rejilla de frecuencias
        spectrum = np.abs(np.fft.rfft(frame, self.fft_size))
        noise = self.noise.get(self.window_name)
        if noise is not None:
            spectrum = np.maximum(spectrum - NOISE_FACTOR * noise, 0.0)
        freqs, amps = self._find_peaks(spectrum)
        notes = self._estimate_notes(freqs, amps)
        if len(freqs) == 0:
            return notes
        evidence = self._evidence_function(freqs, amps)
        # Una nota deducida sólo de sus armónicos (sin fundamental) no es de guitarra
        notes = {n for n in notes
                 if evidence(n) >= (MIN_FUNDAMENTAL_BASS if n < BASS_LIMIT else MIN_FUNDAMENTAL)}
        notes = self._complete_voicing(notes, evidence)
        notes = self._drop_clusters(notes, evidence)
        if len(self.active) >= ESTABLISHED_NOTES:
            # Con el acorde ya sonando, una nota nueva debe tener un pico claramente fuerte
            notes = {n for n in notes if n in self.active or evidence(n) >= LATE_EVIDENCE}
        return notes

    def _evidence_function(self, freqs, amps):
        """midi -> fuerza del pico a esa frecuencia, relativa al pico más fuerte (0..1)."""
        in_range = (freqs >= FMIN) & (freqs <= FMAX)
        top = float(amps[in_range].max()) if in_range.any() else 0.0
        a4 = self.a4

        def evidence(midi):
            if top <= 0:
                return 0.0
            target = a4 * 2 ** ((midi - 69) / 12)
            close = np.abs(1200 * np.log2(freqs / target)) <= PEAK_TOL_CENTS
            return float(amps[close].max() / top) if close.any() else 0.0

        return evidence

    @staticmethod
    def _drop_clusters(notes, evidence):
        """
        Quita notas a un semitono de otra mucho más fuerte: en una guitarra son
        casi siempre picos espurios (intermodulación, ruido) y no notas del acorde.
        """
        strength = {n: evidence(n) for n in notes}
        return {n for n in notes
                if not any(abs(m - n) == 1 and strength[m] >= CLUSTER_RATIO * strength[n]
                           for m in notes)}

    def _complete_voicing(self, notes, evidence):
        """
        Una guitarra toca una sola nota por cuerda y está afinada de forma
        conocida. Con un acorde, la octava de una nota grave se confunde con
        sus propios armónicos (el 2º armónico de G2 es casi tan fuerte como G3
        tocada), así que se asignan las notas a cuerdas reales: para cada
        cuerda se elige la nota con pico claro en el espectro, con trastes
        bajos, y de las clases de nota que ya se detectaron. Así un Sol abierto
        recupera G2 B2 D3 G3 B3 G4 y no sólo la nota grave y la tercera.
        """
        classes = {n % 12 for n in notes}
        if len(classes) < VOICING_MIN_PITCH_CLASSES:
            return notes

        options = []        # (puntuación, cuerda, nota, traste)
        cache = {}
        for string, open_note in enumerate(TUNING):
            for fret in range(MAX_FRET + 1):
                midi = open_note + fret
                if midi % 12 not in classes:
                    continue
                if midi not in cache:
                    cache[midi] = evidence(midi)
                if cache[midi] >= VOICING_EVIDENCE:
                    bonus = BASE_BONUS if midi in notes else 0.0
                    options.append((cache[midi] + bonus - FRET_COST * fret, string, midi, fret))

        chosen, used_strings, used_notes = set(), set(), set()
        for score, string, midi, fret in sorted(options, reverse=True):
            if string in used_strings or midi in used_notes:
                continue
            chosen.add(midi)
            used_strings.add(string)
            used_notes.add(midi)
        # Lo que el estimador ya había detectado nunca se pierde
        return chosen | set(notes)

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
