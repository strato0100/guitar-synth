"""
Seguimiento de notas monofónico.

La estimación de frecuencia (YIN) se hace cada 5 ms y por sí sola salta:
errores de octava en el ataque, lecturas ruidosas cuando la cuerda se
apaga, etc. Este módulo convierte esas lecturas en eventos de nota
estables con una pequeña máquina de estados:

  * Una nota sólo empieza tras `stability` lecturas seguidas que coinciden.
  * Un ataque de púa (onset) abre una ventana en la que se decide si es
    la misma nota (se redispara) o una nota nueva.
  * Sin ataque (ligados, slides) el cambio de nota exige una desviación
    mayor que medio semitono más un margen, mantenida en el tiempo.
  * Los saltos de octava sin ataque se consideran errores y se ignoran,
    salvo al principio de la nota, donde sirven para corregir el ataque.
  * La nota termina cuando el nivel cae bajo la puerta, cuando decae
    demasiado respecto a su pico o cuando se pierde la afinación.
  * Con bends activados, la desviación continua se envía como pitch bend
    y sólo los saltos bruscos (hammer-on, pull-off) cambian de nota.

Eventos generados: ("on", midi), ("off", midi), ("bend", semitonos).
"""

from collections import deque

import numpy as np

from .dsp import FMAX, FMIN, OnsetDetector, Yin, db, freq_to_midi, hop_size, note_name, rms

# Tiempos expresados en pasos del análisis (~5.3 ms, 128 muestras a 24 kHz)
SKIP_AFTER_ONSET = 4        # esperar a que la ventana de análisis vea el ataque
ONSET_WINDOW = 30           # tiempo máximo para decidir tras un ataque (~160 ms)
RELEASE_HOPS = 4            # pasos bajo la puerta para soltar la nota
LOST_HOPS = 45              # pasos sin afinación clara para soltar (~240 ms)
EARLY_HOPS = 20             # al principio de la nota se permiten correcciones de octava
JUMP_LOOKBACK = 4           # para distinguir bend (lento) de ligado (brusco)

GATE_HYSTERESIS_DB = 6.0    # la puerta se cierra 6 dB por debajo de donde abre
SUSTAIN_RANGE_DB = 40.0     # soltar si la nota cae 40 dB bajo su pico
CHANGE_MARGIN = 0.65        # semitonos de desviación para considerar otra nota
OCTAVE_ERRORS = {12, -12, 24, -24, 19, -19}


class MonoTracker:
    def __init__(self, samplerate, params):
        self.samplerate = samplerate
        self.hop = hop_size(samplerate)
        self.yin = Yin(samplerate)
        self.level_len = int(samplerate * 0.0213)     # ventana del nivel (~21 ms)
        self.onset_len = int(samplerate * 0.0107)     # ventana del ataque (~11 ms)
        self.buffer = np.zeros(max(self.yin.size, self.level_len), dtype=np.float32)
        self._pending = np.empty(0, dtype=np.float32)
        self.onsets = OnsetDetector(self.hop / samplerate)
        self.set_params(params)
        self.reset()

    def set_params(self, params):
        self.gate_db = float(params["gate_db"])
        self.onset_db = float(params["onset_db"])
        self.stability = int(params["stability"])
        self.min_confidence = float(params["confidence"])
        self.bend_enabled = bool(params["bend_enabled"])
        self.bend_range = float(params["bend_range"])
        self.a4 = float(params["a4"])

    def reset(self):
        self.note = None
        self.note_age = 0
        self.peak_db = -180.0
        self.release_count = 0
        self.lost_count = 0
        self.skip = 0
        self.onset_timer = 0
        self.cand = None
        self.cand_count = 0
        self.change_cand = None
        self.change_count = 0
        self.last_bend = 0.0
        self.smoothed = None
        self.history = deque(maxlen=JUMP_LOOKBACK + 1)
        self.onsets.reset()
        self.display = {"note": None, "cents": None, "freq": None,
                        "confidence": 0.0, "level_db": -180.0}

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
        onset = self.onsets.update(db(rms(x[-self.onset_len:])), self.onset_db, self.gate_db)

        active = self.note is not None
        gate = self.gate_db - (GATE_HYSTERESIS_DB if active else 0.0)
        if level_db > gate:
            freq, confidence = self.yin(x)
        else:
            freq, confidence = 0.0, 0.0     # en silencio no se gasta CPU en YIN
        voiced = (level_db > gate and confidence >= self.min_confidence
                  and FMIN <= freq <= FMAX)
        m = freq_to_midi(freq, self.a4) if voiced else None
        self.history.append(m)

        if onset:
            self.skip = SKIP_AFTER_ONSET
            self.onset_timer = ONSET_WINDOW
            self._reset_candidates()

        if active:
            self.note_age += 1
            self._check_release(events, level_db, voiced)
            active = self.note is not None

        if self.onset_timer > 0:
            self.onset_timer -= 1

        if voiced and self.skip == 0:
            if not active:
                self._track_start(events, m)
            elif self.onset_timer > 0:
                self._track_reattack(events, m)
            else:
                self._track_legato(events, m)
        if self.skip > 0:
            self.skip -= 1

        self._update_display(m, freq, confidence, level_db)

    # ------------------------------------------------------------------

    def _reset_candidates(self):
        self.cand = None
        self.cand_count = 0
        self.change_cand = None
        self.change_count = 0

    def _count_candidate(self, r):
        if r == self.cand:
            self.cand_count += 1
        else:
            self.cand = r
            self.cand_count = 1
        return self.cand_count >= self.stability

    def _check_release(self, events, level_db, voiced):
        self.peak_db = max(self.peak_db, level_db)
        gate = self.gate_db - GATE_HYSTERESIS_DB
        self.release_count = self.release_count + 1 if level_db < gate else 0
        self.lost_count = 0 if voiced else self.lost_count + 1

        if (self.release_count >= RELEASE_HOPS
                or level_db < self.peak_db - SUSTAIN_RANGE_DB
                or self.lost_count >= LOST_HOPS):
            self._note_off(events)

    def _track_start(self, events, m):
        r = round(m)
        if self._count_candidate(r):
            self._note_on(events, r, m)

    def _track_reattack(self, events, m):
        # Tras un ataque: misma nota -> redisparar, distinta -> cambiar
        r = round(m)
        if self._count_candidate(r):
            self._note_off(events)
            self._note_on(events, r, m)

    def _track_legato(self, events, m):
        dev = m - self.note
        r = round(m)
        interval = r - self.note

        if self.bend_enabled:
            if self.change_cand is None:
                wants_change = (abs(dev) > self.bend_range + 0.5
                                or (abs(dev) >= CHANGE_MARGIN and self._is_jump(m)))
            else:
                # Ya hay un ligado en curso: seguir contando mientras se mantenga
                wants_change = r == self.change_cand
        else:
            wants_change = abs(dev) >= CHANGE_MARGIN

        early = self.note_age <= EARLY_HOPS
        if wants_change and (interval not in OCTAVE_ERRORS or early):
            if r == self.change_cand:
                self.change_count += 1
            else:
                self.change_cand = r
                self.change_count = 1
            # Las correcciones de octava exigen algo más de evidencia
            needed = self.stability + (3 if interval in OCTAVE_ERRORS else 1)
            if self.change_count >= needed:
                self._note_off(events)
                self._note_on(events, r, m)
            return

        self.change_cand = None
        self.change_count = 0
        if self.bend_enabled:
            self._update_bend(events, m)

    def _is_jump(self, m):
        """True si la afinación ha saltado en pocos ms (ligado, no bend)."""
        if len(self.history) <= JUMP_LOOKBACK:
            return False
        previous = self.history[0]
        return (previous is not None
                and abs(previous - self.note) < 0.5
                and abs(m - previous) > 0.75)

    def _update_bend(self, events, m):
        self.smoothed = m if self.smoothed is None else 0.5 * self.smoothed + 0.5 * m
        target = self.smoothed - self.note
        target = max(-self.bend_range, min(self.bend_range, target))
        if abs(target) < 0.05:
            target = 0.0
        if abs(target - self.last_bend) >= 0.02:
            self.last_bend = target
            events.append(("bend", target))

    def _note_on(self, events, r, m):
        if self.bend_enabled and self.last_bend != 0.0:
            events.append(("bend", 0.0))
        self.last_bend = 0.0
        self.smoothed = m
        self.note = r
        self.note_age = 0
        self.peak_db = -180.0
        self.release_count = 0
        self.lost_count = 0
        self.onset_timer = 0    # el ataque ya se ha usado para esta nota
        self._reset_candidates()
        events.append(("on", r))

    def _note_off(self, events):
        if self.note is not None:
            events.append(("off", self.note))
        self.note = None
        self._reset_candidates()

    def _update_display(self, m, freq, confidence, level_db):
        if self.note is not None:
            cents = (m - self.note) * 100 if m is not None else None
            self.display = {"note": note_name(self.note), "midi": self.note,
                            "cents": cents, "freq": freq if m is not None else None,
                            "confidence": confidence, "level_db": level_db}
        else:
            self.display = {"note": None, "midi": None, "cents": None,
                            "freq": freq if m is not None else None,
                            "confidence": confidence, "level_db": level_db}
