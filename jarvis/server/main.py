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

from .activity import ActivityMonitor
from .autonomy import Autonomy
from .cognition import Cognition
from .config import CONFIG, PROJECT_ROOT
from .connectors import email as email_connector
from .core import Jarvis
from .events import BUS
from .learning import Learning
from .memory import Memory
from .notify import NOTIFIER
from .tools import ToolRegistry
from .triage import TriageEngine

WEB_DIR = PROJECT_ROOT / "web"

memory = Memory(CONFIG.db_path)
connector = email_connector.from_config(CONFIG)
tools = ToolRegistry(memory, connector)
jarvis = Jarvis(memory, tools)
activity = ActivityMonitor(memory)
learning = Learning(jarvis, activity)
triage = TriageEngine(memory, connector, jarvis.client)
autonomy = Autonomy(jarvis, triage, activity=activity, learning=learning)
cognition = Cognition(jarvis, autonomy)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    if CONFIG.cognition_enabled:
        cognition.start()
    activity.start()
    BUS.emit(
        "boot",
        name=CONFIG.assistant_name,
        online=jarvis.online,
        model=jarvis.model if jarvis.online else "offline",
        backend=jarvis.backend,
        local=jarvis.local,
        safe_mode=CONFIG.safe_mode,
        email=triage.status(),
        activity=activity.status(),
    )
    if jarvis.local:
        # A local model that isn't pulled yet fails every call with a 404,
        # which reads as "JARVIS is broken" rather than "run one command".
        async def check_local() -> None:
            probe = await jarvis.client.probe()
            if probe.get("ok"):
                BUS.emit("backend_status", ok=True,
                         detail=f"{jarvis.model} ready locally")
            else:
                BUS.emit("backend_status", ok=False, detail=probe.get("error", ""))
                BUS.emit("error", message=f"Local model: {probe.get('error')}")

        asyncio.create_task(check_local())
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
        await activity.stop()
        if hasattr(jarvis.client, "aclose"):
            await jarvis.client.aclose()
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


@app.get("/api/recall")
async def recall(q: str = "", limit: int = 20) -> JSONResponse:
    """Search everything it remembers. Backs the memory search box."""
    query = q.strip()
    if not query:
        return JSONResponse({"hits": [], "query": ""})
    hits = await memory.recall(query, limit)
    return JSONResponse({
        "query": query,
        "hits": [
            {"kind": h.kind, "text": h.text, "score": round(h.score, 3),
             "when": h.ts, "rendered": h.render()}
            for h in hits
        ],
    })


@app.post("/api/remember")
async def remember(payload: dict[str, Any]) -> JSONResponse:
    """Quick capture: store a fact without spending a conversation turn."""
    content = str(payload.get("content", "")).strip()
    if not content:
        return JSONResponse({"error": "content required"}, status_code=400)
    fact_id = await memory.add_fact(
        str(payload.get("subject", "")).strip() or content[:60],
        content,
        importance=float(payload.get("importance", 0.6)),
        source="user",
    )
    BUS.emit("memory", op="store", detail=content[:90])
    return JSONResponse({"ok": True, "id": fact_id})


@app.post("/api/goal")
async def add_goal(payload: dict[str, Any]) -> JSONResponse:
    title = str(payload.get("title", "")).strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    goal_id = await memory.add_goal(title, str(payload.get("detail", "")))
    BUS.emit("goal", id=goal_id, title=title)
    return JSONResponse({"ok": True, "id": goal_id})


@app.get("/api/today")
async def today() -> JSONResponse:
    """Everything the dashboard shows, in one round trip."""
    emails = await memory.list_emails(limit=30, unhandled_only=True)
    tasks = await memory.list_tasks(20)
    return JSONResponse({
        "state": jarvis.state,
        "time": {k: round(v, 1) for k, v in (await activity.today()).items()},
        "play": {
            "minutes": round(await activity.play_minutes(), 1),
            "limit": CONFIG.play_limit_minutes,
        },
        "goals": await memory.list_goals("open"),
        "reminders": await memory.list_reminders(),
        "mail": {
            "unhandled": len(emails),
            "urgent": len([e for e in emails if e["priority"] >= 3]),
            "top": emails[:4],
        },
        "work": {
            "running": autonomy.working_on,
            "pending": len([t for t in tasks if t["status"] == "pending"]),
            "done_today": autonomy.completed,
        },
        "insights": await memory.list_insights(6),
        "observations": await memory.recent_observations(5),
    })


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


# -- what you're doing ---------------------------------------------------


@app.get("/api/activity")
async def activity_view() -> JSONResponse:
    return JSONResponse(
        {
            "monitor": activity.status(),
            "today": await activity.today(),
            "play_minutes": round(await activity.play_minutes(), 1),
            "play_limit": CONFIG.play_limit_minutes,
            "week": await memory.activity_breakdown(time.time() - 7 * 86400, 15),
            "insights": await memory.list_insights(30),
            "notifier": NOTIFIER.status(),
        }
    )


@app.post("/api/insight/{insight_id}/forget")
async def forget_insight(insight_id: int) -> JSONResponse:
    """Delete something it concluded about you. Wrong beliefs must be erasable."""
    ok = await memory.forget_insight(insight_id)
    if ok:
        await memory.add_feedback(
            subject=f"insight #{insight_id}",
            signal="rejected",
            detail="User deleted this conclusion.",
        )
    return JSONResponse({"ok": ok})


@app.post("/api/nudge/snooze")
async def snooze(payload: dict[str, Any]) -> JSONResponse:
    minutes = float(payload.get("minutes", 60))
    NOTIFIER.snooze(minutes)
    return JSONResponse({"ok": True, "muted_for": round(minutes * 60)})


@app.post("/api/nudge/feedback")
async def nudge_feedback(payload: dict[str, Any]) -> JSONResponse:
    """Tell it whether a nudge was worth it, so the next one is better."""
    signal = str(payload.get("signal", "")).strip().lower()
    if signal not in ("helpful", "rejected"):
        return JSONResponse({"error": "signal must be helpful or rejected"},
                            status_code=400)
    await memory.add_feedback(
        subject="nudge", signal=signal, detail=str(payload.get("text", ""))[:400]
    )
    return JSONResponse({"ok": True})


@app.post("/api/learning/mine")
async def mine_now() -> JSONResponse:
    """Run a learning pass immediately instead of waiting hours for one."""
    written = await learning.mine()
    return JSONResponse({"ok": True, "insights": written})


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
        # Replayed history is tagged so the browser can fill its panels
        # without re-firing anything transient. Without this an old nudge
        # or briefing pops up and speaks itself every time the HUD opens.
        history = [{**event, "replay": True} for event in BUS.replay()[-40:]]
        await ws.send_text(
            json.dumps([{"kind": "hello", "status": await jarvis.status()}, *history])
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
