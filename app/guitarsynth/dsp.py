"""Utilidades de procesamiento de señal compartidas por los detectores."""

import math
from collections import deque

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F",
              "F#", "G", "G#", "A", "A#", "B"]

# Rango útil de la guitarra: algo por debajo del Mi grave (82.41 Hz)
# hasta el traste 24 de la 1ª cuerda (~1318 Hz).
FMIN = 70.0
FMAX = 1400.0

# Tamaño de paso común a ambos detectores (a la frecuencia interna).
HOP = 256


def freq_to_midi(freq, a4=440.0):
    return 69 + 12 * math.log2(freq / a4)


def note_name(midi):
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def next_pow2(n):
    return 1 << (int(n) - 1).bit_length()


def db(rms):
    return 20 * math.log10(rms) if rms > 1e-9 else -180.0


def rms(x):
    return float(np.sqrt(np.mean(x * x)))


class Decimator:
    """
    Reduce 96 kHz (o 88.2 kHz) a la mitad promediando pares de muestras.
    A esas frecuencias el promedio basta como filtro antialiasing para una
    guitarra, y el análisis cuesta la mitad de CPU.
    """

    def __init__(self, samplerate):
        self.factor = 2 if samplerate >= 88200 else 1
        self.samplerate = samplerate / self.factor
        self._leftover = np.empty(0, dtype=np.float32)

    def process(self, x):
        if self.factor == 1:
            return x
        if len(self._leftover):
            x = np.concatenate((self._leftover, x))
        usable = len(x) - len(x) % self.factor
        self._leftover = x[usable:].copy()
        return x[:usable].reshape(-1, self.factor).mean(axis=1).astype(np.float32)


class Yin:
    """
    Estimador de frecuencia fundamental YIN (de Cheveigné y Kawahara, 2002)
    calculado con FFT. Devuelve (frecuencia, confianza 0..1).
    """

    def __init__(self, samplerate, fmin=FMIN, fmax=FMAX, window=None, threshold=0.2):
        self.samplerate = samplerate
        self.tau_min = max(2, int(samplerate / fmax))
        self.tau_max = int(samplerate / fmin) + 2
        # ~42 ms: unas 3.5 periodos del Mi grave
        self.window = window or next_pow2(samplerate * 0.04)
        self.size = self.window + self.tau_max     # muestras necesarias
        self.nfft = next_pow2(self.size + self.window)
        self.threshold = threshold
        self._taus = np.arange(self.tau_max + 1)

    def __call__(self, x):
        w, tmax = self.window, self.tau_max
        x = np.asarray(x[-self.size:], dtype=np.float64)

        # r[tau] = sum_{j<W} x[j] * x[j + tau], vía convolución con FFT
        spec = np.fft.rfft(x, self.nfft) * np.fft.rfft(x[:w][::-1], self.nfft)
        r = np.fft.irfft(spec, self.nfft)[w - 1:w + tmax]

        sq = np.concatenate(([0.0], np.cumsum(x * x)))
        energy = sq[self._taus + w] - sq[self._taus]
        d = energy[0] + energy - 2 * r
        d[0] = 0.0

        # Función de diferencia normalizada acumulada (CMNDF)
        cmnd = np.ones_like(d)
        cumulative = np.cumsum(d[1:])
        cmnd[1:] = d[1:] * self._taus[1:] / np.maximum(cumulative, 1e-12)

        segment = cmnd[self.tau_min:tmax]
        below = np.nonzero(segment < self.threshold)[0]
        if len(below):
            tau = below[0] + self.tau_min
            while tau + 1 < tmax and cmnd[tau + 1] < cmnd[tau]:
                tau += 1
        else:
            tau = int(np.argmin(segment)) + self.tau_min

        # Interpolación parabólica para precisión por debajo de una muestra
        a, b, c = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom > 0 else 0.0
        confidence = float(min(1.0, max(0.0, 1.0 - b)))
        return self.samplerate / (tau + shift), confidence


class OnsetDetector:
    """
    Detecta el ataque de la púa: una subida brusca de nivel respecto al
    mínimo de los últimos ~40 ms. Tras un ataque espera ~80 ms antes de
    aceptar otro, para no disparar varias veces en el mismo golpe.
    """

    def __init__(self, hop_seconds):
        self._history = deque(maxlen=max(2, round(0.04 / hop_seconds)))
        self._cooldown_hops = max(1, round(0.08 / hop_seconds))
        self._cooldown = 0

    def reset(self):
        self._history.clear()
        self._cooldown = 0

    def update(self, level_db, threshold_db, gate_db):
        reference = min(self._history) if self._history else -180.0
        self._history.append(level_db)
        if self._cooldown > 0:
            self._cooldown -= 1
            return False
        if level_db > gate_db and level_db - reference >= threshold_db:
            self._cooldown = self._cooldown_hops
            return True
        return False
