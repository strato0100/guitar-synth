"""Ajustes de la app y presets, guardados en JSON en app/data/."""

import json
import os
import threading

DEFAULTS = {
    # Modo y salida
    "mode": "mono",             # mono | poly
    "output": "both",           # synth | guitar | both
    # Sonido
    "engine": "fluidsynth",     # fluidsynth | sfizz
    "sf2": "",                  # SoundFont elegida (ruta); vacío = la de por defecto
    "sfz": "",                  # instrumento SFZ elegido (ruta)
    "program": 0,               # instrumento General MIDI 0..127
    "transpose": 0,             # semitonos
    "volume": 100,              # CC7
    "reverb": 40,               # CC91 (envío de reverb)
    "room": 0.5,                # tamaño de sala de la reverb de FluidSynth
    "chorus": 0,                # CC93 (envío de chorus)
    "velocity": 100,
    "bend_enabled": False,
    "bend_range": 2,            # semitonos de pitch bend
    # Detección
    "gate_db": -60.0,           # nivel mínimo de señal
    "onset_db": 6.0,            # subida de nivel que cuenta como ataque
    "stability": 3,             # lecturas coincidentes antes de aceptar una nota
    "confidence": 0.75,         # confianza mínima de YIN (modo mono)
    "salience": 0.25,           # fuerza mínima de una nota (modo acordes)
    "poly_window": "fast",      # fast | precise
    "poly_hold": 80,            # ms que se esperan para emitir el acorde junto
    "a4": 440.0,                # afinación de referencia
}

SCHEMA = {
    "mode": ("choice", ["mono", "poly"]),
    "output": ("choice", ["synth", "guitar", "both"]),
    "engine": ("choice", ["fluidsynth", "sfizz"]),
    "sf2": ("str", 500),
    "sfz": ("str", 500),
    "program": ("int", 0, 127),
    "transpose": ("int", -36, 36),
    "volume": ("int", 0, 127),
    "reverb": ("int", 0, 127),
    "room": ("float", 0.0, 1.0),
    "chorus": ("int", 0, 127),
    "velocity": ("int", 1, 127),
    "bend_enabled": ("bool",),
    "bend_range": ("int", 1, 12),
    "gate_db": ("float", -80.0, -20.0),
    "onset_db": ("float", 2.0, 20.0),
    "stability": ("int", 1, 10),
    "confidence": ("float", 0.3, 0.98),
    "salience": ("float", 0.05, 0.8),
    "poly_window": ("choice", ["fast", "precise"]),
    "poly_hold": ("int", 0, 300),
    "a4": ("float", 415.0, 466.0),
}

DETECTION_KEYS = ["gate_db", "onset_db", "stability", "confidence",
                  "salience", "poly_window", "poly_hold", "a4"]

PRESET_KEYS = ["mode", "engine", "sf2", "sfz", "program", "transpose", "volume", "reverb", "room",
               "chorus", "velocity", "bend_enabled", "bend_range"]

DEFAULT_PRESETS = {
    "Piano": {"engine": "fluidsynth", "program": 0, "transpose": 0, "reverb": 40, "chorus": 0},
    "Bajo eléctrico (octava abajo)": {"engine": "fluidsynth", "program": 33, "transpose": -12, "reverb": 10, "chorus": 0},
    "Cuerdas": {"engine": "fluidsynth", "program": 48, "transpose": 0, "reverb": 70, "chorus": 20, "mode": "poly"},
    "Órgano Hammond": {"engine": "fluidsynth", "program": 16, "transpose": 0, "reverb": 30, "chorus": 40},
    "Saxo tenor": {"engine": "fluidsynth", "program": 66, "transpose": 0, "reverb": 40, "bend_enabled": True},
    "Flauta": {"engine": "fluidsynth", "program": 73, "transpose": 12, "reverb": 60, "bend_enabled": True},
    "Lead sintetizado": {"engine": "fluidsynth", "program": 81, "transpose": 0, "reverb": 30, "chorus": 30,
                         "bend_enabled": True},
    "Pad cálido": {"engine": "fluidsynth", "program": 89, "transpose": 0, "reverb": 90, "chorus": 50, "mode": "poly"},
}


def _coerce(key, value):
    spec = SCHEMA[key]
    kind = spec[0]
    if kind == "choice":
        if value not in spec[1]:
            raise ValueError(f"{key}: valor no válido {value!r}")
        return value
    if kind == "bool":
        return bool(value)
    if kind == "str":
        return str(value)[:spec[1]]
    number = int(round(float(value))) if kind == "int" else float(value)
    return min(spec[2], max(spec[1], number))


class SettingsStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.settings_path = os.path.join(data_dir, "settings.json")
        self.presets_path = os.path.join(data_dir, "presets.json")
        self.lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

        self.settings = dict(DEFAULTS)
        stored = self._load(self.settings_path, {})
        for key, value in stored.items():
            if key in SCHEMA:
                try:
                    self.settings[key] = _coerce(key, value)
                except (ValueError, TypeError):
                    pass

        self.presets = self._load(self.presets_path, None)
        if self.presets is None:
            self.presets = {name: dict(values) for name, values in DEFAULT_PRESETS.items()}
            self._save(self.presets_path, self.presets)

    @staticmethod
    def _load(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def snapshot(self):
        with self.lock:
            return dict(self.settings)

    def update(self, changes):
        """Valida y aplica cambios. Devuelve sólo los valores que cambiaron."""
        applied = {}
        with self.lock:
            for key, value in changes.items():
                if key not in SCHEMA:
                    continue
                value = _coerce(key, value)
                if self.settings[key] != value:
                    self.settings[key] = value
                    applied[key] = value
            if applied:
                self._save(self.settings_path, self.settings)
        return applied

    def detection_defaults(self):
        return {key: DEFAULTS[key] for key in DETECTION_KEYS}

    # --- presets -------------------------------------------------------

    def list_presets(self):
        with self.lock:
            return {name: dict(values) for name, values in self.presets.items()}

    def save_preset(self, name):
        name = str(name).strip()[:60]
        if not name:
            raise ValueError("El preset necesita un nombre")
        with self.lock:
            self.presets[name] = {key: self.settings[key] for key in PRESET_KEYS}
            self._save(self.presets_path, self.presets)

    def preset_values(self, name):
        with self.lock:
            if name not in self.presets:
                raise ValueError(f"No existe el preset {name!r}")
            return dict(self.presets[name])

    def delete_preset(self, name):
        with self.lock:
            if self.presets.pop(name, None) is not None:
                self._save(self.presets_path, self.presets)
