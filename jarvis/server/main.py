"""HTTP + WebSocket surface. Serves the HUD and relays the event stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .autonomy import Autonomy
from .cognition import Cognition
from .config import CONFIG, PROJECT_ROOT
from .connectors import email as email_connector
from .core import Jarvis
from .events import BUS
from .memory import Memory
from .tools import ToolRegistry
from .triage import TriageEngine

WEB_DIR = PROJECT_ROOT / "web"

memory = Memory(CONFIG.db_path)
connector = email_connector.from_config(CONFIG)
tools = ToolRegistry(memory, connector)
jarvis = Jarvis(memory, tools)
triage = TriageEngine(memory, connector, jarvis.client)
autonomy = Autonomy(jarvis, triage)
cognition = Cognition(jarvis, autonomy)


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
        email=triage.status(),
    )
    if connector is not None and jarvis.online:
        # Verify the mailbox once at boot so a bad password surfaces now
        # rather than silently failing every poll.
        async def check_mail() -> None:
            probe = await connector.probe()
            BUS.emit(
                "email_status",
                ok=bool(probe.get("ok")),
                detail=probe.get("error") or f"{connector.address} reachable",
            )
            if not probe.get("ok"):
                BUS.emit("error", message=f"Email: {probe.get('error')}")

        asyncio.create_task(check_mail())
    try:
        yield
    finally:
        await cognition.stop()
        memory.close()


app = FastAPI(title=f"{CONFIG.assistant_name} Core", lifespan=lifespan)


# -- API -----------------------------------------------------------------


@app.get("/api/health")
async def health() -> JSONResponse:
    """Cheap liveness probe used by the launcher and the autostart service."""
    return JSONResponse(
        {
            "ok": True,
            "name": CONFIG.assistant_name,
            "online": jarvis.online,
            "state": jarvis.state,
            "reflections": cognition.reflections,
        }
    )


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


# -- email ---------------------------------------------------------------


@app.get("/api/inbox")
async def inbox() -> JSONResponse:
    return JSONResponse(
        {
            "email": triage.status(),
            "messages": await memory.list_emails(limit=40, unhandled_only=False),
            "drafts": await memory.list_drafts("pending"),
        }
    )


@app.post("/api/email/sweep")
async def sweep_now() -> JSONResponse:
    """Check mail immediately instead of waiting for the poll interval."""
    if not triage.enabled:
        return JSONResponse(
            {"ok": False, "error": "email is not configured"}, status_code=400
        )
    records = await triage.sweep()
    return JSONResponse({"ok": True, "triaged": len(records)})


@app.post("/api/email/handled/{email_id}")
async def mark_handled(email_id: int) -> JSONResponse:
    return JSONResponse({"ok": await memory.mark_email_handled(email_id)})


@app.post("/api/draft/{draft_id}/{decision}")
async def decide_draft(draft_id: int, decision: str) -> JSONResponse:
    """Approve or discard an outbound draft.

    Approval is the only path by which anything JARVIS wrote ever leaves the
    machine, and it always originates from a click here.
    """
    if decision not in ("approve", "discard"):
        return JSONResponse({"error": "decision must be approve or discard"},
                            status_code=400)

    draft = await memory.get_draft(draft_id)
    if draft is None:
        return JSONResponse({"error": f"no draft #{draft_id}"}, status_code=404)
    if draft["status"] != "pending":
        return JSONResponse(
            {"error": f"draft #{draft_id} is already {draft['status']}"},
            status_code=409,
        )

    if decision == "discard":
        await memory.set_draft_status(draft_id, "discarded")
        BUS.emit("draft", id=draft_id, status="discarded")
        return JSONResponse({"ok": True, "status": "discarded"})

    if not CONFIG.email_allow_send:
        return JSONResponse(
            {
                "error": "sending is disabled. Set JARVIS_EMAIL_ALLOW_SEND=1 in"
                         " jarvis/.env and restart to enable it."
            },
            status_code=403,
        )
    if connector is None or not connector.configured:
        return JSONResponse({"error": "email is not configured"}, status_code=400)

    try:
        await connector.send(draft["to_addr"], draft["subject"], draft["body"])
    except Exception as exc:  # noqa: BLE001 - report the real reason
        BUS.emit("error", message=f"Send failed: {exc}")
        return JSONResponse({"error": str(exc)}, status_code=502)

    await memory.set_draft_status(draft_id, "sent")
    BUS.emit("draft", id=draft_id, status="sent", to=draft["to_addr"])
    return JSONResponse({"ok": True, "status": "sent"})


# -- work and ventures ---------------------------------------------------


@app.get("/api/work")
async def work() -> JSONResponse:
    return JSONResponse(
        {
            "autonomy": autonomy.status(),
            "tasks": await memory.list_tasks(25),
            "ventures": await memory.list_ventures(),
        }
    )


@app.post("/api/work/queue")
async def queue_work(payload: dict[str, Any]) -> JSONResponse:
    title = str(payload.get("title", "")).strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    task_id = await memory.queue_task(
        title, str(payload.get("detail", "")), origin="user", priority=2
    )
    BUS.emit("work_queued", id=task_id, title=title, origin="user")
    return JSONResponse({"ok": True, "id": task_id})


@app.post("/api/routine/{name}")
async def run_routine(name: str) -> JSONResponse:
    """Trigger a scheduled routine on demand (briefing, ventures, worker)."""
    for routine in autonomy.routines:
        if routine.name == name:
            routine.last_run = time.time()
            asyncio.create_task(routine.run())
            return JSONResponse({"ok": True, "routine": name})
    return JSONResponse({"error": f"no routine '{name}'"}, status_code=404)


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
