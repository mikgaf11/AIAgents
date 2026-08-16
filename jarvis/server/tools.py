"""The hands: every capability JARVIS can reach for.

Local tools execute here; web search and web fetch are declared as
Anthropic server-side tools and run on Anthropic's infrastructure, so
they never appear in this registry's dispatch table.
"""

from __future__ import annotations

import ast
import asyncio
import getpass
import io
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import CONFIG
from .memory import Memory

try:  # optional, richer telemetry when available
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None


Handler = Callable[[dict[str, Any]], Awaitable[Any]]

# Commands the model may run unattended in safe mode. Anything else needs
# safe mode turned off, which is an explicit choice by the operator.
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "file", "stat",
    "echo", "pwd", "date", "uptime", "df", "du", "free", "ps", "whoami",
    "python3", "python", "pip", "node", "npm", "git", "which", "env",
    "uname", "hostname", "sort", "uniq", "cut", "sed", "awk", "tree", "curl",
}

SHELL_METACHARS = re.compile(r"[;&|`$><\n]")


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    # Tools flagged dangerous are surfaced differently in the HUD so the
    # operator can see when JARVIS reaches for something with side effects.
    dangerous: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolError(Exception):
    """Raised by a handler to return an is_error tool_result to the model."""


class ToolRegistry:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.tools: dict[str, Tool] = {}
        self._register_all()

    # -- registry --------------------------------------------------------

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def specs(self) -> list[dict[str, Any]]:
        """Local tool definitions plus Anthropic's server-side tools."""
        specs = [t.spec() for t in self.tools.values()]
        if CONFIG.enable_web:
            specs.append({"type": "web_search_20260209", "name": "web_search"})
            specs.append({"type": "web_fetch_20260209", "name": "web_fetch"})
        return specs

    def is_local(self, name: str) -> bool:
        return name in self.tools

    async def execute(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a local tool. Returns (rendered_result, is_error)."""
        tool = self.tools.get(name)
        if tool is None:
            return f"Unknown tool: {name}", True
        try:
            result = await tool.handler(args or {})
        except ToolError as exc:
            return str(exc), True
        except Exception as exc:  # noqa: BLE001 - surface to the model
            return f"{type(exc).__name__}: {exc}", True
        if isinstance(result, str):
            return result, False
        return json.dumps(result, indent=2, default=str), False

    # -- path safety -----------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        """Resolve a model-supplied path, jailed to the workspace in safe mode."""
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = CONFIG.workspace / candidate
        resolved = candidate.resolve()
        if CONFIG.safe_mode:
            root = CONFIG.workspace.resolve()
            if resolved != root and root not in resolved.parents:
                raise ToolError(
                    f"Path {resolved} is outside the workspace ({root})."
                    " Safe mode confines file access to the workspace."
                )
        return resolved

    # -- registration ----------------------------------------------------

    def _register_all(self) -> None:
        self._register_time()
        self._register_memory()
        self._register_goals()
        self._register_reminders()
        self._register_files()
        self._register_exec()
        self._register_system()

    # ---- time ----------------------------------------------------------

    def _register_time(self) -> None:
        async def now(_: dict[str, Any]) -> dict[str, Any]:
            local = datetime.now().astimezone()
            return {
                "iso": local.isoformat(timespec="seconds"),
                "human": local.strftime("%A, %d %B %Y at %H:%M:%S %Z"),
                "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "epoch": time.time(),
            }

        self.add(
            Tool(
                name="get_time",
                description=(
                    "Get the current local date and time. Call this whenever the"
                    " answer depends on 'now' — scheduling, elapsed time, or"
                    " anything the user describes as today/tonight/tomorrow."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=now,
            )
        )

    # ---- memory --------------------------------------------------------

    def _register_memory(self) -> None:
        async def remember(args: dict[str, Any]) -> str:
            subject = str(args.get("subject", "")).strip()
            content = str(args.get("content", "")).strip()
            if not subject or not content:
                raise ToolError("Both 'subject' and 'content' are required.")
            fact_id = await self.memory.add_fact(
                subject,
                content,
                kind=str(args.get("kind", "fact")),
                importance=float(args.get("importance", 0.6)),
            )
            return f"Stored as fact #{fact_id} under subject '{subject}'."

        async def recall(args: dict[str, Any]) -> str:
            query = str(args.get("query", "")).strip()
            if not query:
                raise ToolError("'query' is required.")
            limit = int(args.get("limit", 8))
            hits = await self.memory.recall(query, limit)
            if not hits:
                return "No stored memories matched that query."
            return "\n".join(h.render() for h in hits)

        async def forget(args: dict[str, Any]) -> str:
            fact_id = int(args.get("fact_id", 0))
            ok = await self.memory.forget_fact(fact_id)
            return f"Fact #{fact_id} deleted." if ok else f"No fact #{fact_id}."

        self.add(
            Tool(
                name="remember",
                description=(
                    "Persist a durable fact about the user, their projects, or"
                    " their preferences so it survives across sessions. Use it"
                    " when you learn something that would change how you answer"
                    " later — not for transient chat content."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": "Short key, e.g. 'user.timezone'.",
                        },
                        "content": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["fact", "preference", "project", "person", "skill"],
                        },
                        "importance": {
                            "type": "number",
                            "description": "0-1; how much this should outrank other recalls.",
                        },
                    },
                    "required": ["subject", "content"],
                },
                handler=remember,
            )
        )
        self.add(
            Tool(
                name="recall",
                description=(
                    "Search long-term memory (stored facts and past conversation)"
                    " by keyword. Call it before saying you don't know something"
                    " about the user or a past discussion."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                handler=recall,
            )
        )
        self.add(
            Tool(
                name="forget",
                description="Delete a stored fact by id when it is wrong or obsolete.",
                input_schema={
                    "type": "object",
                    "properties": {"fact_id": {"type": "integer"}},
                    "required": ["fact_id"],
                },
                handler=forget,
                dangerous=True,
            )
        )

    # ---- goals ---------------------------------------------------------

    def _register_goals(self) -> None:
        async def set_goal(args: dict[str, Any]) -> str:
            title = str(args.get("title", "")).strip()
            if not title:
                raise ToolError("'title' is required.")
            goal_id = await self.memory.add_goal(
                title,
                str(args.get("detail", "")),
                int(args.get("priority", 3)),
            )
            return f"Goal #{goal_id} opened: {title}"

        async def list_goals(args: dict[str, Any]) -> Any:
            status = args.get("status", "open")
            return await self.memory.list_goals(None if status == "all" else status)

        async def update_goal(args: dict[str, Any]) -> str:
            goal_id = int(args.get("goal_id", 0))
            ok = await self.memory.update_goal(
                goal_id,
                status=args.get("status"),
                progress=args.get("progress"),
                detail=args.get("detail"),
            )
            return f"Goal #{goal_id} updated." if ok else f"No goal #{goal_id}."

        self.add(
            Tool(
                name="set_goal",
                description=(
                    "Open a standing objective you should keep working toward"
                    " across sessions. Goals are reviewed by your background"
                    " cognition loop even when the user is away."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "priority": {
                            "type": "integer",
                            "description": "1 highest, 5 lowest.",
                        },
                    },
                    "required": ["title"],
                },
                handler=set_goal,
            )
        )
        self.add(
            Tool(
                name="list_goals",
                description="List tracked goals and their progress.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["open", "done", "dropped", "all"],
                        }
                    },
                },
                handler=list_goals,
            )
        )
        self.add(
            Tool(
                name="update_goal",
                description="Change a goal's status, progress (0-1), or detail.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "integer"},
                        "status": {
                            "type": "string",
                            "enum": ["open", "done", "dropped"],
                        },
                        "progress": {"type": "number"},
                        "detail": {"type": "string"},
                    },
                    "required": ["goal_id"],
                },
                handler=update_goal,
            )
        )

    # ---- reminders -----------------------------------------------------

    def _register_reminders(self) -> None:
        async def set_reminder(args: dict[str, Any]) -> str:
            text = str(args.get("text", "")).strip()
            if not text:
                raise ToolError("'text' is required.")
            if "in_seconds" in args and args["in_seconds"] is not None:
                due = time.time() + float(args["in_seconds"])
            elif args.get("at_iso"):
                try:
                    parsed = datetime.fromisoformat(str(args["at_iso"]))
                except ValueError as exc:
                    raise ToolError(f"Could not parse at_iso: {exc}") from exc
                if parsed.tzinfo is None:
                    parsed = parsed.astimezone()
                due = parsed.timestamp()
            else:
                raise ToolError("Provide either 'in_seconds' or 'at_iso'.")
            rid = await self.memory.add_reminder(text, due)
            when = datetime.fromtimestamp(due).astimezone()
            return f"Reminder #{rid} set for {when:%Y-%m-%d %H:%M %Z}: {text}"

        async def list_reminders(_: dict[str, Any]) -> Any:
            items = await self.memory.list_reminders()
            for item in items:
                item["due_human"] = datetime.fromtimestamp(item["due"]).isoformat(
                    timespec="minutes"
                )
            return items

        self.add(
            Tool(
                name="set_reminder",
                description=(
                    "Schedule something for you to raise later. When it comes"
                    " due you will be woken with it, even mid-idle."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "in_seconds": {"type": "number"},
                        "at_iso": {
                            "type": "string",
                            "description": "Local ISO timestamp, e.g. 2026-08-17T09:30",
                        },
                    },
                    "required": ["text"],
                },
                handler=set_reminder,
            )
        )
        self.add(
            Tool(
                name="list_reminders",
                description="List reminders that have not fired yet.",
                input_schema={"type": "object", "properties": {}},
                handler=list_reminders,
            )
        )

    # ---- files ---------------------------------------------------------

    def _register_files(self) -> None:
        async def read_file(args: dict[str, Any]) -> str:
            path = self._resolve(str(args.get("path", "")))
            if not path.is_file():
                raise ToolError(f"Not a file: {path}")
            max_bytes = int(args.get("max_bytes", 200_000))
            data = path.read_bytes()[:max_bytes]
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return f"<binary file, {len(data)} bytes read>"
            return text

        async def write_file(args: dict[str, Any]) -> str:
            path = self._resolve(str(args.get("path", "")))
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and args.get("mode") == "append":
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(content)
                return f"Appended {len(content)} chars to {path}"
            path.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} chars to {path}"

        async def list_dir(args: dict[str, Any]) -> str:
            path = self._resolve(str(args.get("path", ".")))
            if not path.is_dir():
                raise ToolError(f"Not a directory: {path}")
            lines = []
            for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                kind = "dir " if entry.is_dir() else "file"
                size = entry.stat().st_size if entry.is_file() else 0
                lines.append(f"{kind} {size:>10}  {entry.name}")
            return "\n".join(lines) or "<empty>"

        async def search_files(args: dict[str, Any]) -> str:
            root = self._resolve(str(args.get("path", ".")))
            pattern = str(args.get("pattern", ""))
            if not pattern:
                raise ToolError("'pattern' is required.")
            regex = re.compile(pattern)
            hits: list[str] = []
            for file in root.rglob(str(args.get("glob", "*"))):
                if not file.is_file() or file.stat().st_size > 2_000_000:
                    continue
                try:
                    for lineno, line in enumerate(
                        file.read_text(encoding="utf-8", errors="ignore").splitlines(),
                        1,
                    ):
                        if regex.search(line):
                            hits.append(f"{file}:{lineno}: {line.strip()[:200]}")
                            if len(hits) >= 100:
                                return "\n".join(hits) + "\n<truncated at 100 hits>"
                except OSError:
                    continue
            return "\n".join(hits) or "No matches."

        self.add(
            Tool(
                name="read_file",
                description="Read a UTF-8 text file from disk.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_bytes": {"type": "integer"},
                    },
                    "required": ["path"],
                },
                handler=read_file,
            )
        )
        self.add(
            Tool(
                name="write_file",
                description=(
                    "Create or overwrite a text file. Use mode 'append' to add"
                    " to an existing file instead of replacing it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {"type": "string", "enum": ["write", "append"]},
                    },
                    "required": ["path", "content"],
                },
                handler=write_file,
                dangerous=True,
            )
        )
        self.add(
            Tool(
                name="list_dir",
                description="List the contents of a directory.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
                handler=list_dir,
            )
        )
        self.add(
            Tool(
                name="search_files",
                description="Regex-search file contents beneath a directory.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "pattern": {"type": "string"},
                        "glob": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
                handler=search_files,
            )
        )

    # ---- execution -----------------------------------------------------

    def _register_exec(self) -> None:
        async def run_python(args: dict[str, Any]) -> str:
            code = str(args.get("code", ""))
            if not code.strip():
                raise ToolError("'code' is required.")
            # Reject imports of modules that would let sandboxed code walk
            # out of the workspace when safe mode is on.
            if CONFIG.safe_mode:
                try:
                    tree = ast.parse(code)
                except SyntaxError as exc:
                    raise ToolError(f"SyntaxError: {exc}") from exc
                banned = {"socket", "subprocess", "ctypes", "multiprocessing"}
                for node in ast.walk(tree):
                    names: list[str] = []
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module.split(".")[0]]
                    hit = banned.intersection(names)
                    if hit:
                        raise ToolError(
                            f"Safe mode blocks importing {sorted(hit)} inside"
                            " run_python. Use the shell tool or ask the operator"
                            " to disable safe mode."
                        )

            def _exec() -> str:
                out, err = io.StringIO(), io.StringIO()
                env: dict[str, Any] = {"__name__": "__jarvis__"}
                cwd = os.getcwd()
                try:
                    os.chdir(CONFIG.workspace)
                    with redirect_stdout(out), redirect_stderr(err):
                        exec(compile(code, "<jarvis>", "exec"), env)  # noqa: S102
                except Exception:  # noqa: BLE001 - report back to the model
                    err.write(traceback.format_exc())
                finally:
                    os.chdir(cwd)
                stdout, stderr = out.getvalue(), err.getvalue()
                parts = []
                if stdout:
                    parts.append(f"stdout:\n{stdout}")
                if stderr:
                    parts.append(f"stderr:\n{stderr}")
                return "\n".join(parts) if parts else "<no output>"

            return await asyncio.to_thread(_exec)

        async def shell(args: dict[str, Any]) -> str:
            command = str(args.get("command", "")).strip()
            if not command:
                raise ToolError("'command' is required.")
            if CONFIG.safe_mode:
                if SHELL_METACHARS.search(command):
                    raise ToolError(
                        "Safe mode rejects shell operators (; | & ` $ > <)."
                        " Run one plain command per call."
                    )
                program = command.split()[0]
                if program not in SAFE_COMMANDS:
                    raise ToolError(
                        f"Safe mode allows only {sorted(SAFE_COMMANDS)};"
                        f" '{program}' is not on the allowlist."
                    )
                argv = command.split()
            else:
                argv = ["/bin/sh", "-c", command]

            def _run() -> str:
                proc = subprocess.run(  # noqa: S603
                    argv,
                    cwd=str(CONFIG.workspace),
                    capture_output=True,
                    text=True,
                    timeout=CONFIG.shell_timeout,
                )
                chunks = [f"exit={proc.returncode}"]
                if proc.stdout:
                    chunks.append(f"stdout:\n{proc.stdout[:20000]}")
                if proc.stderr:
                    chunks.append(f"stderr:\n{proc.stderr[:8000]}")
                return "\n".join(chunks)

            try:
                return await asyncio.to_thread(_run)
            except subprocess.TimeoutExpired:
                raise ToolError(
                    f"Command exceeded {CONFIG.shell_timeout}s and was killed."
                ) from None
            except FileNotFoundError as exc:
                raise ToolError(str(exc)) from exc

        self.add(
            Tool(
                name="run_python",
                description=(
                    "Execute Python in-process and return stdout/stderr. Use it"
                    " for calculation, data wrangling, and quick analysis rather"
                    " than doing arithmetic in your head."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
                handler=run_python,
                dangerous=True,
            )
        )
        self.add(
            Tool(
                name="shell",
                description=(
                    "Run a shell command in the workspace directory and return"
                    " its exit code and output."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                handler=shell,
                dangerous=True,
            )
        )

    # ---- system --------------------------------------------------------

    def _register_system(self) -> None:
        async def system_status(_: dict[str, Any]) -> dict[str, Any]:
            return await asyncio.to_thread(collect_system_status)

        self.add(
            Tool(
                name="system_status",
                description=(
                    "Read host telemetry: CPU load, memory, disk, uptime,"
                    " platform. Use it when the user asks how the machine is"
                    " doing or before recommending a heavy operation."
                ),
                input_schema={"type": "object", "properties": {}},
                handler=system_status,
            )
        )


def collect_system_status() -> dict[str, Any]:
    """Host telemetry, degrading gracefully when psutil isn't installed."""
    status: dict[str, Any] = {
        "platform": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "cpu_count": os.cpu_count(),
    }
    try:
        status["user"] = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in some containers
        status["user"] = "unknown"

    if psutil is not None:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(CONFIG.workspace))
        status.update(
            {
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": vm.percent,
                "memory_used_gb": round(vm.used / 1e9, 2),
                "memory_total_gb": round(vm.total / 1e9, 2),
                "disk_percent": disk.percent,
                "disk_free_gb": round(disk.free / 1e9, 2),
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            }
        )
        battery = getattr(psutil, "sensors_battery", lambda: None)()
        if battery is not None:
            status["battery_percent"] = round(battery.percent, 1)
            status["on_ac_power"] = bool(battery.power_plugged)
        return status

    try:
        load1, load5, load15 = os.getloadavg()
        cores = os.cpu_count() or 1
        status["load_avg"] = [round(load1, 2), round(load5, 2), round(load15, 2)]
        status["cpu_percent"] = round(min(100.0, 100.0 * load1 / cores), 1)
    except (OSError, AttributeError):
        pass
    usage = shutil.disk_usage(str(CONFIG.workspace))
    status["disk_percent"] = round(100.0 * usage.used / usage.total, 1)
    status["disk_free_gb"] = round(usage.free / 1e9, 2)
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values = {}
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            values[key] = float(rest.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0.0)
        available = values.get("MemAvailable", 0.0)
        if total:
            status["memory_total_gb"] = round(total / 1e9, 2)
            status["memory_used_gb"] = round((total - available) / 1e9, 2)
            status["memory_percent"] = round(100.0 * (total - available) / total, 1)
    status["note"] = "install psutil for richer telemetry"
    return status
