"""HTTP + WebSocket surface. Serves the HUD and relays the event stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .cognition import Cognition
from .config import CONFIG, PROJECT_ROOT
from .core import Jarvis
from .events import BUS
from .memory import Memory
from .tools import ToolRegistry

WEB_DIR = PROJECT_ROOT / "web"

memory = Memory(CONFIG.db_path)
tools = ToolRegistry(memory)
jarvis = Jarvis(memory, tools)
cognition = Cognition(jarvis)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    if CONFIG.cognition_enabled:
        cognition.start()
    BUS.emit(
        "boot",
        name=CONFIG.assistant_name,
        online=jarvis.online,
        model=CONFIG.model if jarvis.online else "offline",
        safe_mode=CONFIG.safe_mode,
    )
    try:
        yield
    finally:
        await cognition.stop()
        memory.close()


app = FastAPI(title=f"{CONFIG.assistant_name} Core", lifespan=lifespan)


# -- API -----------------------------------------------------------------


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(await jarvis.status())


@app.post("/api/message")
async def message(payload: dict[str, Any]) -> JSONResponse:
    text = str(payload.get("text", "")).strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    reply = await jarvis.respond(text, spoken=bool(payload.get("spoken")))
    return JSONResponse({"reply": reply})


@app.get("/api/memory")
async def memory_dump() -> JSONResponse:
    return JSONResponse(
        {
            "facts": await memory.all_facts(200),
            "goals": await memory.list_goals(None),
            "reminders": await memory.list_reminders(),
            "observations": await memory.recent_observations(25),
            "stats": await memory.stats(),
        }
    )


@app.post("/api/cognition/tick")
async def force_tick() -> JSONResponse:
    """Run one reflection pass now instead of waiting for the timer."""
    await cognition.tick()
    return JSONResponse({"ok": True, "ticks": cognition.ticks, "mood": cognition.mood})


# -- WebSocket -----------------------------------------------------------


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    queue = BUS.subscribe()

    async def pump() -> None:
        """Forward bus events to the browser, coalescing token bursts."""
        try:
            while True:
                event = await queue.get()
                batch = [event]
                # Draining whatever else is queued keeps token streaming from
                # turning into one websocket frame per character.
                while len(batch) < 64:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await ws.send_text(json.dumps(batch))
        except (WebSocketDisconnect, RuntimeError):
            return

    pump_task = asyncio.create_task(pump())
    try:
        await ws.send_text(
            json.dumps(
                [
                    {"kind": "hello", "status": await jarvis.status()},
                    *BUS.replay()[-40:],
                ]
            )
        )
        while True:
            raw = await ws.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            action = payload.get("action")
            if action == "message":
                text = str(payload.get("text", "")).strip()
                if text:
                    # Fire and forget so the socket keeps pumping telemetry
                    # while the turn runs.
                    asyncio.create_task(
                        jarvis.respond(text, spoken=bool(payload.get("spoken")))
                    )
            elif action == "activity":
                # The browser heard the user moving/talking; reset idle timers
                # so the background mind doesn't interrupt.
                jarvis.last_user_activity = payload.get("t") or jarvis.last_user_activity
            elif action == "tick":
                asyncio.create_task(cognition.tick())
            elif action == "status":
                BUS.emit("status", status=await jarvis.status())
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        BUS.unsubscribe(queue)


# -- static HUD ----------------------------------------------------------


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="web")


def run() -> None:
    import uvicorn

    print(f"\n  {CONFIG.assistant_name} online  ->  http://{CONFIG.host}:{CONFIG.port}\n")
    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port, log_level="warning")


if __name__ == "__main__":
    run()
