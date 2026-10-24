#!/usr/bin/env python3
"""
Pasa un WAV por el mismo detector que usa la app y muestra las notas.

Permite probar ajustes de detección sin tocar en directo: graba con
tools/record.py y analiza aquí con distintos valores.

Uso:
    python3 app/tools/analyze.py notas.wav
    python3 app/tools/analyze.py acordes.wav --mode poly
    python3 app/tools/analyze.py notas.wav --stability 4 --gate-db -55 --bend

Por defecto usa los ajustes guardados por la app (app/data/settings.json);
las opciones de la línea de comandos los sustituyen.
"""

import argparse
import os
import sys
import wave

import numpy as np

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

from guitarsynth.chords import identify_chord  # noqa: E402
from guitarsynth.dsp import Decimator, note_name  # noqa: E402
from guitarsynth.settings import SettingsStore  # noqa: E402


def read_wav(path):
    with wave.open(path, "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
                | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        data = ints.astype(np.float32) / (1 << 23)
    else:
        sys.exit(f"Formato WAV no soportado ({width * 8} bits)")
    if channels > 1:
        data = data.reshape(-1, channels)[:, 0]   # canal izquierdo = guitarra
    return data, rate


def main():
    parser = argparse.ArgumentParser(description="Analiza un WAV con el detector de la app.")
    parser.add_argument("wav")
    parser.add_argument("--mode", choices=["mono", "poly"], help="por defecto, el de la app")
    parser.add_argument("--gate-db", type=float)
    parser.add_argument("--onset-db", type=float)
    parser.add_argument("--stability", type=int)
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--salience", type=float)
    parser.add_argument("--poly-window", choices=["fast", "precise"])
    parser.add_argument("--bend", action="store_true", help="activar seguimiento de bends")
    parser.add_argument("--data-dir", default=os.path.join(APP_DIR, "data"))
    args = parser.parse_args()

    settings = SettingsStore(args.data_dir).snapshot()
    for key in ("mode", "gate_db", "onset_db", "stability", "confidence", "salience", "poly_window"):
        value = getattr(args, key)
        if value is not None:
            settings[key] = value
    if args.bend:
        settings["bend_enabled"] = True

    data, rate = read_wav(args.wav)
    decimator = Decimator(rate)
    samples = decimator.process(data)
    sr = decimator.samplerate

    if settings["mode"] == "poly":
        from guitarsynth.poly import PolyTracker
        tracker = PolyTracker(sr, settings)
    else:
        from guitarsynth.mono import MonoTracker
        tracker = MonoTracker(sr, settings)

    print(f"{args.wav}: {len(data) / rate:.1f} s a {rate} Hz, modo {settings['mode']}")
    print("Ajustes: " + ", ".join(f"{k}={settings[k]}" for k in
                                  ("gate_db", "onset_db", "stability", "confidence", "salience",
                                   "poly_window", "bend_enabled")))
    print(f"Pico de la grabación: {20 * np.log10(np.max(np.abs(data)) + 1e-9):.1f} dBFS\n")

    block = 128
    active = set()
    counts = {"on": 0, "off": 0, "bend": 0}
    last_chord = None
    for i in range(0, len(samples), block):
        t = i / sr
        for kind, value in tracker.process(samples[i:i + block]):
            counts[kind] += 1
            if kind == "bend":
                continue
            if kind == "on":
                active.add(value)
            else:
                active.discard(value)
            line = f"{t:8.3f} s  {kind:3s} {note_name(value):4s}"
            if settings["mode"] == "poly":
                chord = identify_chord(sorted(active))
                name = chord["name"] if chord else ("-" if not active else "?")
                if name != last_chord:
                    line += f"   acorde: {name}"
                    last_chord = name
            print(line)

    print(f"\nTotal: {counts['on']} notas, {counts['bend']} mensajes de bend")


if __name__ == "__main__":
    main()
