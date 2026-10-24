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

# El análisis trabaja a ~24 kHz: una guitarra no necesita más (fundamentales
# hasta 1.4 kHz) y cada FFT cuesta una cuarta parte que a 96 kHz.
ANALYSIS_RATE = 24000
HOP_SECONDS = 0.00533


def hop_size(samplerate):
    """Paso del análisis (~5.3 ms): 128 muestras a 24 kHz."""
    return max(64, round(samplerate * HOP_SECONDS / 64) * 64)


def freq_to_midi(freq, a4=440.0):
    return 69 + 12 * math.log2(freq / a4)


def note_name(midi):
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def next_pow2(n):
    return 1 << (int(n) - 1).bit_length()


def db(rms):
    return 20 * math.log10(rms) if rms > 1e-9 else -180.0


def rms(x):
    # np.dot es una sola llamada sin copias; np.mean(x * x) cuesta varias veces más
    return math.sqrt(float(np.dot(x, x)) / len(x)) if len(x) else 0.0


class AudioRing:
    """
    Búfer circular entre el callback de JACK (productor) y el hilo de análisis
    (consumidor). Escribir cuesta una copia de memoria y nada más: sin colas,
    sin bloqueos y sin avisar al otro hilo, porque el callback de JACK se
    ejecuta cientos de veces por segundo y cada microsegundo cuenta.
    """

    def __init__(self, size=1 << 17):
        self.size = size
        self.data = np.zeros(size, dtype=np.float32)
        self.written = 0
        self.consumed = 0
        self.overruns = 0       # veces que el análisis llegó tarde y se perdió audio

    def write(self, block):
        n = len(block)
        start = self.written % self.size
        end = start + n
        if end <= self.size:
            self.data[start:end] = block
        else:
            first = self.size - start
            self.data[start:] = block[:first]
            self.data[:n - first] = block[first:]
        self.written += n

    def available(self):
        return self.written - self.consumed

    def take(self):
        """Devuelve todo el audio pendiente (copia) y lo marca como leído."""
        written = self.written
        if written - self.consumed > self.size // 2:
            self.overruns += 1
            self.consumed = written - self.size // 4    # quedarse con lo más reciente
        start = self.consumed % self.size
        count = written - self.consumed
        end = start + count
        if end <= self.size:
            out = self.data[start:end].copy()
        else:
            out = np.concatenate((self.data[start:], self.data[:end - self.size]))
        self.consumed = written
        return out


class Decimator:
    """
    Reduce la frecuencia de muestreo hasta ~24 kHz promediando pares de
    muestras en varias etapas (96 kHz -> 48 kHz -> 24 kHz). Dos filtros de
    caja en cascada atenúan lo bastante lo que queda por encima de 12 kHz
    para el análisis de notas.
    """

    def __init__(self, samplerate):
        self.stages = 0
        rate = float(samplerate)
        while rate / 2 >= ANALYSIS_RATE * 0.8:
            rate /= 2
            self.stages += 1
        self.samplerate = rate
        self._leftover = [np.empty(0, dtype=np.float32) for _ in range(self.stages)]

    def process(self, x):
        for stage in range(self.stages):
            if len(self._leftover[stage]):
                x = np.concatenate((self._leftover[stage], x))
            usable = len(x) - len(x) % 2
            self._leftover[stage] = x[usable:].copy()
            x = (x[:usable:2] + x[1:usable:2]) * 0.5
        return x.astype(np.float32, copy=False)


class Yin:
    """
    Estimador de frecuencia fundamental YIN (de Cheveigné y Kawahara, 2002)
    calculado con FFT. Devuelve (frecuencia, confianza 0..1).
    """

    def __init__(self, samplerate, fmin=FMIN, fmax=FMAX, window=None, threshold=0.2):
        self.samplerate = samplerate
        self.tau_min = max(2, int(samplerate / fmax))
        self.tau_max = int(samplerate / fmin) + 2
        # ~32 ms: más de 2.5 periodos del Mi grave (82 Hz) y una FFT de la mitad de tamaño
        self.window = window or max(256, int(samplerate * 0.032))
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
