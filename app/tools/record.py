#!/usr/bin/env python3
"""
Graba la guitarra desde JACK a un archivo WAV (mono, 32 bits float).

Sirve para calibrar el detector con tu guitarra real: graba unas notas
o acordes y analízalos después con tools/analyze.py.

Uso:
    python3 app/tools/record.py notas.wav --seconds 20
    python3 app/tools/record.py acordes.wav --seconds 30 --port system:capture_1

No hace falta parar la app: ambos pueden leer la entrada a la vez.
"""

import argparse
import queue
import sys
import time
import wave

import jack
import numpy as np


def main():
    parser = argparse.ArgumentParser(description="Graba la guitarra desde JACK a WAV.")
    parser.add_argument("output", help="archivo WAV de salida")
    parser.add_argument("--seconds", type=float, default=20.0, help="duración (por defecto 20 s)")
    parser.add_argument("--port", default="system:capture_1", help="puerto de captura JACK")
    args = parser.parse_args()

    audio = queue.Queue()
    client = jack.Client("guitar-synth-record")
    inport = client.inports.register("input")

    @client.set_process_callback
    def process(frames):
        audio.put(inport.get_array().copy())

    with client:
        client.connect(args.port, inport)
        samplerate = client.samplerate
        total = int(args.seconds * samplerate)
        print(f"Grabando {args.seconds:.0f} s de {args.port} a {samplerate} Hz… (Ctrl+C para parar antes)")
        chunks, count = [], 0
        start = time.monotonic()
        try:
            while count < total:
                block = audio.get(timeout=2)
                chunks.append(block)
                count += len(block)
                elapsed = time.monotonic() - start
                peak = float(np.max(np.abs(block)))
                print(f"\r  {elapsed:5.1f} s   pico {20 * np.log10(peak + 1e-9):6.1f} dBFS ", end="", flush=True)
        except KeyboardInterrupt:
            pass
        except queue.Empty:
            sys.exit("\nNo llega audio de JACK.")
        print()

    data = np.concatenate(chunks)[:total].astype(np.float32)
    # WAV PCM 16 bits (lo entiende cualquier programa); se normaliza si satura
    peak = float(np.max(np.abs(data))) if len(data) else 0.0
    scale = 32767 / peak if peak > 1.0 else 32767
    pcm = np.clip(data * scale, -32768, 32767).astype("<i2")
    with wave.open(args.output, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm.tobytes())
    print(f"Guardado {args.output}: {len(data) / samplerate:.1f} s, pico {20 * np.log10(peak + 1e-9):.1f} dBFS")


if __name__ == "__main__":
    main()
