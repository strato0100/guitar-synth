"""
Servidor web: sirve la interfaz y la conecta al motor por WebSocket.

Mensajes del navegador al servidor:
  {"type": "set", "values": {...}}          cambiar ajustes
  {"type": "detection.reset"}               ajustes de detección por defecto
  {"type": "preset.save", "name": "..."}
  {"type": "preset.load", "name": "..."}
  {"type": "preset.delete", "name": "..."}
  {"type": "library.rescan"}                buscar de nuevo archivos .sf2/.sfz
  {"type": "panic"}

Mensajes del servidor al navegador:
  {"type": "init", settings, presets, instruments, categories, library, status}
  {"type": "library", "dirs": [...], "engines": [...]}
  {"type": "notice", "text": "...", "error": bool}
  {"type": "settings", "settings": {...}}
  {"type": "presets", "presets": {...}}
  {"type": "live", ...}                     nota/acorde y nivel, ~20 veces/s
  {"type": "status", ...}
  {"type": "error", "message": "..."}
"""

import asyncio
import logging
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .instruments import category_list, instrument_list

log = logging.getLogger("server")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
MEDIA_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}

LIVE_INTERVAL = 0.05        # 20 actualizaciones por segundo
STATUS_EVERY = 40           # estado (MODEP, FluidSynth) cada ~2 s


def create_app(engine):
    app = FastAPI(title="Guitar Synth", docs_url=None, redoc_url=None)
    clients = set()
    state = {"broadcaster": None}

    def static_file(name):
        path = os.path.join(STATIC_DIR, name)
        with open(path, "rb") as f:
            content = f.read()
        media = MEDIA_TYPES.get(os.path.splitext(name)[1], "application/octet-stream")
        return Response(content, media_type=media, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    def index():
        return static_file("index.html")

    @app.get("/static/{name}")
    def static(name: str):
        if "/" in name or ".." in name or not os.path.isfile(os.path.join(STATIC_DIR, name)):
            return Response(status_code=404)
        return static_file(name)

    async def send(ws, message):
        try:
            await ws.send_json(message)
        except Exception:
            clients.discard(ws)

    async def broadcast(message):
        await asyncio.gather(*(send(ws, message) for ws in list(clients)))

    async def broadcaster():
        tick = 0
        seen_rev = engine.settings_rev
        seen_notice = engine.notice["id"]
        while clients:
            await broadcast(dict(engine.live_state(), type="live"))
            if engine.settings_rev != seen_rev:
                seen_rev = engine.settings_rev
                await broadcast({"type": "settings", "settings": engine.store.snapshot()})
            if engine.notice["id"] != seen_notice:
                seen_notice = engine.notice["id"]
                await broadcast(dict(engine.notice, type="notice"))
                await broadcast(dict(engine.status(), type="status"))
            tick += 1
            if tick % STATUS_EVERY == 0:
                await asyncio.get_running_loop().run_in_executor(None, engine.refresh_routing)
                await broadcast(dict(engine.status(), type="status"))
            await asyncio.sleep(LIVE_INTERVAL)
        state["broadcaster"] = None

    async def handle(ws, message):
        kind = message.get("type")
        store = engine.store
        loop = asyncio.get_running_loop()

        if kind == "set":
            await loop.run_in_executor(None, engine.apply, message.get("values") or {})
            await broadcast({"type": "settings", "settings": store.snapshot()})
        elif kind == "detection.reset":
            await loop.run_in_executor(None, engine.apply, store.detection_defaults())
            await broadcast({"type": "settings", "settings": store.snapshot()})
        elif kind == "preset.save":
            store.save_preset(message.get("name", ""))
            await broadcast({"type": "presets", "presets": store.list_presets()})
        elif kind == "preset.load":
            values = store.preset_values(message.get("name", ""))
            await loop.run_in_executor(None, engine.apply, values)
            await broadcast({"type": "settings", "settings": store.snapshot()})
        elif kind == "preset.delete":
            store.delete_preset(message.get("name", ""))
            await broadcast({"type": "presets", "presets": store.list_presets()})
        elif kind == "library.rescan":
            await loop.run_in_executor(None, engine.rescan)
            await broadcast(dict(engine.library_info(), type="library"))
        elif kind == "panic":
            engine.panic()
        else:
            raise ValueError(f"Mensaje desconocido: {kind!r}")

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        await send(ws, {
            "type": "init",
            "settings": engine.store.snapshot(),
            "presets": engine.store.list_presets(),
            "instruments": instrument_list(),
            "categories": category_list(),
            "library": engine.library_info(),
            "status": engine.status(),
        })
        if state["broadcaster"] is None:
            state["broadcaster"] = asyncio.ensure_future(broadcaster())
        try:
            while True:
                message = await ws.receive_json()
                try:
                    await handle(ws, message)
                except (ValueError, TypeError) as exc:
                    await send(ws, {"type": "error", "message": str(exc)})
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    return app
