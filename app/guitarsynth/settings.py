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

# Presets de fábrica de la versión anterior: se quitan si el usuario no los tocó.
LEGACY_PRESETS = {
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


def _preset(program, transpose=0, reverb=40, chorus=0, bend=False, poly=False):
    """
    Preset General MIDI. `bend` indica si el instrumento real sigue bends y
    vibrato (cuerdas frotadas, vientos, voces, guitarras, leads…): con True la
    afinación continua de la guitarra se envía al synth. En modo Acordes no hay
    bends, así que esos presets los dejan en False.
    """
    return {"engine": "fluidsynth", "mode": "poly" if poly else "mono",
            "program": program, "transpose": transpose, "reverb": reverb,
            "chorus": chorus, "bend_enabled": bend and not poly, "bend_range": 2}


DEFAULT_PRESETS = {
    # --- Teclados ---
    "Teclas · Piano de cola": _preset(0, reverb=40),
    "Teclas · Piano brillante": _preset(1, reverb=35),
    "Teclas · Rhodes": _preset(4, reverb=35, chorus=30),
    "Teclas · Piano eléctrico FM": _preset(5, reverb=30, chorus=30),
    "Teclas · Clavecín": _preset(6, reverb=30),
    "Teclas · Clavinet": _preset(7, reverb=10),
    "Teclas · Órgano Hammond": _preset(16, reverb=30, chorus=40),
    "Teclas · Órgano de iglesia": _preset(19, reverb=90, poly=True),
    "Teclas · Acordeón": _preset(21, reverb=30),
    "Teclas · Armónica": _preset(22, reverb=20, bend=True),
    # --- Percusión melódica ---
    "Percusión · Vibráfono": _preset(11, reverb=50, chorus=20),
    "Percusión · Marimba": _preset(12, reverb=35),
    "Percusión · Celesta": _preset(8, transpose=12, reverb=50),
    "Percusión · Caja de música": _preset(10, transpose=12, reverb=50),
    "Percusión · Kalimba": _preset(108, reverb=40),
    # --- Guitarras y cuerdas pulsadas (siguen bends) ---
    "Guitarra · Española (nylon)": _preset(24, reverb=30, bend=True),
    "Guitarra · Acústica (acero)": _preset(25, reverb=30, bend=True),
    "Guitarra · Eléctrica jazz": _preset(26, reverb=35, chorus=25, bend=True),
    "Guitarra · Eléctrica limpia": _preset(27, reverb=30, chorus=30, bend=True),
    "Guitarra · Overdrive": _preset(29, reverb=20, bend=True),
    "Guitarra · Distorsión": _preset(30, reverb=20, bend=True),
    "Cuerda pulsada · Sitar": _preset(104, reverb=40, bend=True),
    "Cuerda pulsada · Banjo": _preset(105, reverb=20),
    "Cuerda pulsada · Koto": _preset(107, reverb=40),
    "Cuerda pulsada · Arpa": _preset(46, reverb=60),
    # --- Bajos (una octava abajo) ---
    "Bajo · Eléctrico (dedos)": _preset(33, transpose=-12, reverb=10, bend=True),
    "Bajo · Eléctrico (púa)": _preset(34, transpose=-12, reverb=10, bend=True),
    "Bajo · Fretless": _preset(35, transpose=-12, reverb=15, bend=True),
    "Bajo · Slap": _preset(36, transpose=-12, reverb=10),
    "Bajo · Contrabajo acústico": _preset(32, transpose=-12, reverb=20, bend=True),
    "Bajo · Sintetizado": _preset(38, transpose=-12, reverb=10),
    # --- Cuerdas frotadas (vibrato y bends) ---
    "Cuerdas · Violín": _preset(40, transpose=12, reverb=50, bend=True),
    "Cuerdas · Viola": _preset(41, transpose=12, reverb=50, bend=True),
    "Cuerdas · Violonchelo": _preset(42, reverb=50, bend=True),
    "Cuerdas · Contrabajo de orquesta": _preset(43, transpose=-12, reverb=50, bend=True),
    "Cuerdas · Violín folk (fiddle)": _preset(110, transpose=12, reverb=35, bend=True),
    "Cuerdas · Pizzicato": _preset(45, reverb=45),
    "Cuerdas · Ensamble": _preset(48, reverb=70, chorus=20, poly=True),
    "Cuerdas · Ensamble sintetizado": _preset(50, reverb=60, chorus=30, poly=True),
    # --- Voces ---
    "Voces · Coro «Aah»": _preset(52, reverb=70, poly=True),
    "Voces · «Ooh»": _preset(53, reverb=70, poly=True),
    "Voces · Voz sintetizada": _preset(54, reverb=50, bend=True),
    # --- Metales ---
    "Metales · Trompeta": _preset(56, reverb=40, bend=True),
    "Metales · Trompeta con sordina": _preset(59, reverb=35, bend=True),
    "Metales · Trombón": _preset(57, reverb=40, bend=True),
    "Metales · Corno francés": _preset(60, reverb=60, bend=True),
    "Metales · Tuba": _preset(58, transpose=-12, reverb=40, bend=True),
    "Metales · Sección": _preset(61, reverb=40, poly=True),
    "Metales · Sintetizados": _preset(62, reverb=40, chorus=20, poly=True),
    # --- Saxos y lengüetas ---
    "Saxos · Soprano": _preset(64, reverb=40, bend=True),
    "Saxos · Alto": _preset(65, reverb=40, bend=True),
    "Saxos · Tenor": _preset(66, reverb=40, bend=True),
    "Saxos · Barítono": _preset(67, transpose=-12, reverb=40, bend=True),
    "Lengüetas · Oboe": _preset(68, transpose=12, reverb=50, bend=True),
    "Lengüetas · Clarinete": _preset(71, transpose=12, reverb=45, bend=True),
    "Lengüetas · Fagot": _preset(70, transpose=-12, reverb=45, bend=True),
    "Lengüetas · Gaita": _preset(109, reverb=25, bend=True),
    # --- Flautas y vientos ---
    "Vientos · Flauta": _preset(73, transpose=12, reverb=60, bend=True),
    "Vientos · Flautín": _preset(72, transpose=12, reverb=55, bend=True),
    "Vientos · Flauta dulce": _preset(74, transpose=12, reverb=50, bend=True),
    "Vientos · Flauta de pan": _preset(75, transpose=12, reverb=60, bend=True),
    "Vientos · Shakuhachi": _preset(77, reverb=60, bend=True),
    "Vientos · Ocarina": _preset(79, transpose=12, reverb=50, bend=True),
    "Vientos · Silbido": _preset(78, transpose=12, reverb=40, bend=True),
    # --- Sintetizadores ---
    "Synth · Lead cuadrada": _preset(80, reverb=30, chorus=30, bend=True),
    "Synth · Lead diente de sierra": _preset(81, reverb=30, chorus=30, bend=True),
    "Synth · Lead calíope": _preset(82, reverb=35, bend=True),
    "Synth · Lead charang": _preset(84, reverb=30, bend=True),
    "Synth · Lead quintas": _preset(86, reverb=30, chorus=20, bend=True),
    "Synth · Pad new age": _preset(88, reverb=85, chorus=40, poly=True),
    "Synth · Pad cálido": _preset(89, reverb=90, chorus=50, poly=True),
    "Synth · Pad coro": _preset(91, reverb=85, chorus=40, poly=True),
    "Synth · Pad halo": _preset(94, reverb=90, chorus=40, poly=True),
    "Synth · Atmósfera": _preset(99, reverb=85, chorus=30, poly=True),
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

        self.seen_path = os.path.join(data_dir, "presets_offered.json")
        self.presets = self._load(self.presets_path, {})
        self._merge_default_presets()

    def _merge_default_presets(self):
        """
        Añade los presets de fábrica que aún no se han ofrecido a este usuario,
        sin tocar los suyos ni volver a crear los que borró. La primera vez
        quita los presets de fábrica antiguos si no se modificaron.
        """
        offered = self._load(self.seen_path, None)
        changed = False
        if offered is None:
            offered = []
            for name, values in LEGACY_PRESETS.items():
                if self.presets.get(name) == values:
                    del self.presets[name]
                    changed = True
        for name, values in DEFAULT_PRESETS.items():
            if name not in offered:
                offered.append(name)
                if name not in self.presets:
                    self.presets[name] = dict(values)
                changed = True
        if changed or not os.path.exists(self.seen_path):
            self._save(self.presets_path, self.presets)
            self._save(self.seen_path, offered)

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
