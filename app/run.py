#!/usr/bin/env python3
"""
Guitar Synth: convierte la guitarra eléctrica en cualquier instrumento
General MIDI en tiempo real (Pisound + JACK + FluidSynth) y se controla
desde el navegador.

Uso:
    python3 app/run.py
    python3 app/run.py --http-port 8080 --port system:capture_1

Después abre http://<ip-de-la-pi>:8080 en el móvil o el PC.
"""

import argparse
import logging
import os
import socket
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)


def check_dependencies():
    missing = []
    for module, package in (("jack", "python3-jack-client"), ("numpy", "python3-numpy"),
                            ("fastapi", "fastapi"), ("uvicorn", "uvicorn")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    try:
        import websockets  # noqa: F401
    except ImportError:
        try:
            import wsproto  # noqa: F401
        except ImportError:
            missing.append("websockets")
    if missing:
        sys.exit("Faltan dependencias: " + ", ".join(missing) +
                 "\nInstálalas con: pip3 install -r app/requirements.txt")


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def parse_args():
    parser = argparse.ArgumentParser(description="Guitar Synth: guitarra -> FluidSynth con control web.")
    parser.add_argument("--port", default="system:capture_1",
                        help="puerto de captura JACK de la guitarra (por defecto system:capture_1)")
    parser.add_argument("--http-port", type=int, default=8080,
                        help="puerto de la interfaz web (por defecto 8080)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="dirección en la que escucha la web (por defecto todas)")
    parser.add_argument("--soundfont", default="/usr/share/sounds/sf2/FluidR3_GM.sf2",
                        help="SoundFont General MIDI para FluidSynth")
    parser.add_argument("--gain", type=float, default=0.7,
                        help="ganancia maestra de FluidSynth (por defecto 0.7)")
    parser.add_argument("--data-dir", default=os.path.join(APP_DIR, "data"),
                        help="carpeta donde se guardan ajustes y presets")
    parser.add_argument("--verbose", action="store_true",
                        help="mostrar más mensajes (incluidos los de FluidSynth)")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    check_dependencies()

    import jack
    import uvicorn

    from guitarsynth.engine import Engine
    from guitarsynth.server import create_app
    from guitarsynth.settings import SettingsStore

    if not os.path.isfile(args.soundfont):
        sys.exit(f"No existe la SoundFont {args.soundfont}")

    store = SettingsStore(args.data_dir)
    engine = Engine(store, capture_port=args.port, soundfont=args.soundfont,
                    fluid_gain=args.gain)

    print("Arrancando motor de audio (cargar la SoundFont tarda unos segundos)...")
    try:
        engine.start()
    except (jack.JackError, RuntimeError) as exc:
        engine.stop()
        sys.exit(f"Error al arrancar: {exc}")

    status = engine.status()
    print(f"Audio: {status['samplerate']} Hz, entrada {args.port}, "
          f"MODEP {'activo' if status['modep'] else 'inactivo'}")
    print(f"Interfaz web: http://{local_ip()}:{args.http_port}  (Ctrl+C para salir)")

    try:
        uvicorn.run(create_app(engine), host=args.host, port=args.http_port,
                    log_level="info" if args.verbose else "warning")
    finally:
        print("Cerrando: restaurando conexiones de JACK y deteniendo FluidSynth...")
        engine.stop()


if __name__ == "__main__":
    main()
