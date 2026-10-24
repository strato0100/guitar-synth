#!/usr/bin/env python3

import math
import queue

import aubio
import jack
import numpy as np

WINDOW_SIZE = 4096
HOP_SIZE = 512

audio_queue = queue.Queue(maxsize=128)


def note_name(freq):
    midi = round(69 + 12 * math.log2(freq / 440.0))
    notes = [
        "C", "C#", "D", "D#", "E", "F",
        "F#", "G", "G#", "A", "A#", "B"
    ]
    return f"{notes[midi % 12]}{midi // 12 - 1}"


client = jack.Client("PitchDiagnostic")
input_port = client.inports.register("input")

detector = aubio.pitch(
    "schmitt",
    WINDOW_SIZE,
    HOP_SIZE,
    client.samplerate
)

detector.set_unit("Hz")
detector.set_silence(-60)

@client.set_process_callback
def process(frames):
    samples = input_port.get_array().copy()
    try:
        audio_queue.put_nowait(samples)
    except queue.Full:
        pass


print(f"Frecuencia de muestreo: {client.samplerate} Hz")

try:
    with client:
        client.connect("system:capture_1", input_port)
        print("Toca únicamente el Mi grave al aire.")
        print("Mantén la cuerda sonando y pulsa Ctrl+C para salir.\n")

        pending = np.empty(0, dtype=np.float32)
        count = 0

        while True:
            try:
                block = audio_queue.get(timeout=1)
                pending = np.concatenate((pending, block))
            except queue.Empty:
                continue

            while len(pending) >= HOP_SIZE:
                frame = pending[:HOP_SIZE]
                pending = pending[HOP_SIZE:]

                rms = float(np.sqrt(np.mean(frame * frame)))
                frequency = float(detector(frame)[0])

                if rms > 0.0001 and 30 <= frequency <= 1000:
                    count += 1

                    if count % 5 == 0:
                        print(
                            f"{frequency:8.2f} Hz  "
                            f"{note_name(frequency):4s}  "
                            f"RMS={rms:.5f}",
                            flush=True
                        )

except KeyboardInterrupt:
    print("\nPrueba finalizada.")

finally:
    client.close()