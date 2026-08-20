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

# Directories that make a home-folder walk take minutes and never hold
# anything the user was looking for.
SKIP_DIRS = {
    "node_modules", "__pycache__", "venv", ".venv", "site-packages", "Library",
    "AppData", "Windows", "System32", "Program Files", "$Recycle.Bin",
    "dist", "build", "target", "vendor", "Caches",
}

AUDIO_SUFFIXES = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma"}

URL_SCHEMES = ("http://", "https://", "spotify:", "file://")


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
    def __init__(self, memory: Memory, connector=None) -> None:
        self.memory = memory
        # The email connector is optional; the mail tools report that it
        # isn't configured rather than disappearing, so the model can tell
        # the user what to do about it.
        self.connector = connector
        self.tools: dict[str, Tool] = {}
        self._register_all()

    # -- registry --------------------------------------------------------

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def specs(
        self,
        names: list[str] | None = None,
        include_server_tools: bool = True,
    ) -> list[dict[str, Any]]:
        """Local tool definitions plus Anthropic's server-side tools.

        `names` restricts the surface — the background worker gets a smaller
        one than the conversational core.
        """
        selected = self.tools.values()
        if names is not None:
            allowed = set(names)
            selected = [t for t in self.tools.values() if t.name in allowed]
        specs = [t.spec() for t in selected]
        if CONFIG.enable_web and include_server_tools:
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
            roots = CONFIG.allowed_roots
            if not any(resolved == r or r in resolved.parents for r in roots):
                allowed = ", ".join(str(r) for r in roots)
                raise ToolError(
                    f"Path {resolved} is outside the folders I'm allowed to touch"
                    f" ({allowed}). Add it to JARVIS_FILE_ROOTS in jarvis/.env to"
                    " grant access."
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
        self._register_desktop()
        self._register_email()
        self._register_work()
        self._register_ventures()

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

    # ---- the desktop ---------------------------------------------------

    def _register_desktop(self) -> None:
        """Finding things, opening things, and playing music.

        These are the tools that make it feel like it is *on* the machine
        rather than in a browser tab. They are still bounded: file search
        walks only the allowed roots, and opening anything is a single
        argv-list call — never a shell string.
        """

        async def find_files(args: dict[str, Any]) -> str:
            name = str(args.get("name", "")).strip()
            if not name:
                raise ToolError("'name' is required — a filename or part of one.")
            needle = name.lower()
            limit = max(1, min(int(args.get("limit", 40)), 200))
            where = str(args.get("path", "")).strip()
            roots = [self._resolve(where)] if where else list(CONFIG.allowed_roots)

            def walk() -> list[str]:
                hits: list[str] = []
                for root in roots:
                    if not root.is_dir():
                        continue
                    for dirpath, dirnames, filenames in os.walk(root):
                        # Skip the caches and version-control noise that make
                        # a home-directory walk take minutes.
                        dirnames[:] = [
                            d for d in dirnames
                            if not d.startswith(".") and d not in SKIP_DIRS
                        ]
                        for filename in filenames:
                            if needle in filename.lower():
                                full = Path(dirpath) / filename
                                try:
                                    size = full.stat().st_size
                                except OSError:
                                    size = 0
                                hits.append(f"{size:>10}  {full}")
                                if len(hits) >= limit:
                                    return hits
                return hits

            found = await asyncio.to_thread(walk)
            if not found:
                searched = ", ".join(str(r) for r in roots)
                return f"No file matching {name!r} under {searched}."
            return "\n".join(found)

        async def open_path(args: dict[str, Any]) -> str:
            if not CONFIG.allow_open:
                raise ToolError(
                    "Opening things is disabled (JARVIS_ALLOW_OPEN=0 in .env)."
                )
            target = str(args.get("target", "")).strip()
            if not target:
                raise ToolError("'target' is required — a file, folder or URL.")
            if _is_url(target):
                opened = target
            else:
                path = self._resolve(target)
                if not path.exists():
                    raise ToolError(f"Nothing at {path}.")
                opened = str(path)
            ok, detail = await asyncio.to_thread(_os_open, opened)
            if not ok:
                raise ToolError(f"Could not open {opened}: {detail}")
            return f"Opened {opened}"

        async def launch_app(args: dict[str, Any]) -> str:
            if not CONFIG.allow_open:
                raise ToolError(
                    "Launching apps is disabled (JARVIS_ALLOW_OPEN=0 in .env)."
                )
            name = str(args.get("name", "")).strip()
            if not name:
                raise ToolError("'name' is required.")
            if SHELL_METACHARS.search(name):
                raise ToolError("App names cannot contain shell metacharacters.")
            ok, detail = await asyncio.to_thread(_launch_app, name)
            if not ok:
                raise ToolError(f"Could not launch {name}: {detail}")
            return f"Launched {name}"

        async def play_music(args: dict[str, Any]) -> str:
            if not CONFIG.allow_open:
                raise ToolError("Playback is disabled (JARVIS_ALLOW_OPEN=0 in .env).")
            query = str(args.get("query", "")).strip()
            if _is_url(query):
                ok, detail = await asyncio.to_thread(_os_open, query)
                return f"Playing {query}" if ok else f"Could not open it: {detail}"

            def search() -> list[Path]:
                matches: list[Path] = []
                needle = query.lower()
                for raw in CONFIG.music_dirs:
                    root = Path(raw).expanduser()
                    if not root.is_dir():
                        continue
                    for file in root.rglob("*"):
                        if (
                            file.is_file()
                            and file.suffix.lower() in AUDIO_SUFFIXES
                            and (not needle or needle in file.name.lower())
                        ):
                            matches.append(file)
                            if len(matches) >= 25:
                                return matches
                return matches

            found = await asyncio.to_thread(search)
            if not found:
                where = ", ".join(CONFIG.music_dirs) or "(no music folders set)"
                raise ToolError(
                    f"No audio matching {query!r} in {where}. Set JARVIS_MUSIC_DIRS"
                    " in .env, or give me a streaming URL instead."
                )
            ok, detail = await asyncio.to_thread(_os_open, str(found[0]))
            if not ok:
                raise ToolError(f"Found {found[0].name} but could not play it: {detail}")
            more = f" ({len(found) - 1} other matches)" if len(found) > 1 else ""
            return f"Playing {found[0].name}{more}"

        async def media_control(args: dict[str, Any]) -> str:
            action = str(args.get("action", "playpause")).lower()
            if action not in ("playpause", "play", "pause", "next", "previous", "stop"):
                raise ToolError("action must be playpause, next, previous or stop.")
            ok, detail = await asyncio.to_thread(_media_key, action)
            if not ok:
                raise ToolError(f"Media control unavailable: {detail}")
            return f"Sent {action} to whatever is playing."

        async def activity_report(args: dict[str, Any]) -> dict[str, Any]:
            hours = float(args.get("hours", 24))
            since = time.time() - hours * 3600
            return {
                "window_hours": hours,
                "by_app": await self.memory.activity_breakdown(since, 20),
                "recent": (await self.memory.recent_activity(15)),
            }

        async def notify_me(args: dict[str, Any]) -> str:
            from .notify import NOTIFIER

            text = str(args.get("text", "")).strip()
            if not text:
                raise ToolError("'text' is required.")
            delivered = await NOTIFIER.nudge(
                text,
                kind=str(args.get("kind", "coach")),
                urgent=bool(args.get("urgent")),
            )
            return (
                "Delivered." if delivered
                else "Suppressed — too soon after the last interruption."
            )

        self.add(Tool(
            name="find_files",
            description=(
                "Find files by name anywhere in the folders you're allowed to"
                " reach. Use this when the user asks where something is, or"
                " asks you to open a file they only half remember."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Filename or part of one."},
                    "path": {"type": "string",
                             "description": "Optional directory to search under."},
                    "limit": {"type": "integer"},
                },
                "required": ["name"],
            },
            handler=find_files,
        ))
        self.add(Tool(
            name="open_path",
            description=(
                "Open a file, folder or URL with whatever application the"
                " system normally uses for it."
            ),
            input_schema={
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
            handler=open_path,
            dangerous=True,
        ))
        self.add(Tool(
            name="launch_app",
            description="Start an application by name, e.g. 'Spotify', 'code'.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            handler=launch_app,
            dangerous=True,
        ))
        self.add(Tool(
            name="play_music",
            description=(
                "Play music: a local track matching a search, or a streaming"
                " URL. Give an empty query to play whatever is found first."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            handler=play_music,
            dangerous=True,
        ))
        self.add(Tool(
            name="media_control",
            description=(
                "Pause, resume or skip whatever is currently playing, without"
                " caring which app is playing it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["playpause", "play", "pause", "next",
                                 "previous", "stop"],
                    }
                },
            },
            handler=media_control,
        ))
        self.add(Tool(
            name="activity_report",
            description=(
                "Where the user's time actually went — minutes per app and"
                " category. Use it before commenting on their habits, so what"
                " you say is grounded in what they did rather than a guess."
            ),
            input_schema={
                "type": "object",
                "properties": {"hours": {"type": "number"}},
            },
            handler=activity_report,
        ))
        self.add(Tool(
            name="notify_me",
            description=(
                "Pop up on screen and say something out loud, even if the HUD"
                " is hidden behind a game. Rate-limited: use it when something"
                " genuinely needs their attention, not to chat."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["coach", "alert", "reminder", "playtime"]},
                    "urgent": {"type": "boolean"},
                },
                "required": ["text"],
            },
            handler=notify_me,
            dangerous=True,
        ))

    # ---- email ---------------------------------------------------------

    def _register_email(self) -> None:
        async def list_emails(args: dict[str, Any]) -> Any:
            rows = await self.memory.list_emails(
                limit=int(args.get("limit", 20)),
                unhandled_only=bool(args.get("unhandled_only", True)),
            )
            minimum = int(args.get("min_priority", 0))
            return [
                {
                    "id": r["id"],
                    "from": r["sender"],
                    "email": r["sender_email"],
                    "subject": r["subject"],
                    "priority": r["priority_label"],
                    "category": r["category"],
                    "summary": r["summary"],
                    "action": r["action"],
                    "needs_reply": bool(r["needs_reply"]),
                    "received": datetime.fromtimestamp(r["received"]).isoformat(
                        timespec="minutes"
                    ) if r["received"] else "",
                }
                for r in rows
                if r["priority"] >= minimum
            ]

        async def read_email(args: dict[str, Any]) -> Any:
            rows = await self.memory.list_emails(limit=200, unhandled_only=False)
            email_id = int(args.get("email_id", 0))
            for row in rows:
                if row["id"] == email_id:
                    return {
                        "from": f'{row["sender"]} <{row["sender_email"]}>',
                        "subject": row["subject"],
                        "received": datetime.fromtimestamp(
                            row["received"]
                        ).isoformat(timespec="minutes") if row["received"] else "",
                        "priority": row["priority_label"],
                        "category": row["category"],
                        "body": row["snippet"],
                    }
            raise ToolError(f"No email #{email_id} in the triaged store.")

        async def search_email(args: dict[str, Any]) -> Any:
            query = str(args.get("query", "")).strip()
            if not query:
                raise ToolError("'query' is required.")
            rows = await self.memory.search_emails(query, int(args.get("limit", 10)))
            return [
                {
                    "id": r["id"], "from": r["sender"], "subject": r["subject"],
                    "priority": r["priority_label"], "summary": r["summary"],
                }
                for r in rows
            ]

        async def draft_reply(args: dict[str, Any]) -> str:
            to_addr = str(args.get("to", "")).strip()
            subject = str(args.get("subject", "")).strip()
            body = str(args.get("body", "")).strip()
            email_id = args.get("email_id")
            if not body:
                raise ToolError("'body' is required.")

            if email_id and not (to_addr and subject):
                rows = await self.memory.list_emails(limit=200, unhandled_only=False)
                for row in rows:
                    if row["id"] == int(email_id):
                        to_addr = to_addr or row["sender_email"]
                        if not subject:
                            original = row["subject"]
                            subject = (
                                original if original.lower().startswith("re:")
                                else f"Re: {original}"
                            )
                        break
            if not to_addr:
                raise ToolError("'to' is required (or a valid 'email_id').")

            draft_id = await self.memory.add_draft(
                to_addr, subject or "(no subject)", body,
                int(email_id) if email_id else None,
            )
            return (
                f"Draft #{draft_id} saved for {to_addr}. It is waiting for the"
                " user's approval in the HUD — nothing has been sent."
            )

        async def mark_handled(args: dict[str, Any]) -> str:
            email_id = int(args.get("email_id", 0))
            ok = await self.memory.mark_email_handled(email_id)
            return f"Email #{email_id} marked handled." if ok else f"No email #{email_id}."

        self.add(
            Tool(
                name="list_emails",
                description=(
                    "List triaged inbox messages with their priority, category and"
                    " one-line summary. This is your view of the user's mail —"
                    " use it before answering anything about their inbox."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                        "unhandled_only": {"type": "boolean"},
                        "min_priority": {
                            "type": "integer",
                            "description": "0 noise, 2 normal, 3 high, 4 critical.",
                        },
                    },
                },
                handler=list_emails,
            )
        )
        self.add(
            Tool(
                name="read_email",
                description="Read the stored text of one triaged email by id.",
                input_schema={
                    "type": "object",
                    "properties": {"email_id": {"type": "integer"}},
                    "required": ["email_id"],
                },
                handler=read_email,
            )
        )
        self.add(
            Tool(
                name="search_email",
                description="Search triaged mail by sender, subject or content.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["query"],
                },
                handler=search_email,
            )
        )
        self.add(
            Tool(
                name="draft_reply",
                description=(
                    "Write a reply and queue it for the user's approval. This never"
                    " sends anything — the user approves or discards it in the HUD."
                    " Pass email_id to reply to a triaged message and the recipient"
                    " and subject are filled in for you."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "email_id": {"type": "integer"},
                        "to": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["body"],
                },
                handler=draft_reply,
            )
        )
        self.add(
            Tool(
                name="mark_email_handled",
                description="Mark a triaged email as dealt with so it stops surfacing.",
                input_schema={
                    "type": "object",
                    "properties": {"email_id": {"type": "integer"}},
                    "required": ["email_id"],
                },
                handler=mark_handled,
            )
        )

    # ---- background work -----------------------------------------------

    def _register_work(self) -> None:
        async def queue_task(args: dict[str, Any]) -> str:
            title = str(args.get("title", "")).strip()
            if not title:
                raise ToolError("'title' is required.")
            task_id = await self.memory.queue_task(
                title,
                str(args.get("detail", "")),
                origin=str(args.get("origin", "conversation")),
                priority=int(args.get("priority", 3)),
            )
            return (
                f"Queued as background task #{task_id}. It will run with tools"
                " and report back when done."
            )

        async def list_tasks(_: dict[str, Any]) -> Any:
            rows = await self.memory.list_tasks(20)
            return [
                {
                    "id": r["id"], "title": r["title"], "status": r["status"],
                    "result": r["result"][:400],
                }
                for r in rows
            ]

        async def note_observation(args: dict[str, Any]) -> str:
            text = str(args.get("text", "")).strip()
            if not text:
                raise ToolError("'text' is required.")
            await self.memory.add_observation(text)
            return "Observation recorded."

        self.add(
            Tool(
                name="queue_task",
                description=(
                    "Queue work for yourself to do in the background, with tools."
                    " Use it when something needs real research or drafting rather"
                    " than an immediate answer, so the user isn't left waiting."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "detail": {
                            "type": "string",
                            "description": "Everything the background run needs;"
                                           " it cannot ask follow-up questions.",
                        },
                        "priority": {"type": "integer", "description": "1 highest, 5 lowest."},
                    },
                    "required": ["title"],
                },
                handler=queue_task,
            )
        )
        self.add(
            Tool(
                name="list_tasks",
                description="See queued, running and finished background work.",
                input_schema={"type": "object", "properties": {}},
                handler=list_tasks,
            )
        )
        self.add(
            Tool(
                name="note_observation",
                description="Record a durable note for your future self.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                handler=note_observation,
            )
        )

    # ---- ventures ------------------------------------------------------

    def _register_ventures(self) -> None:
        async def propose_venture(args: dict[str, Any]) -> str:
            title = str(args.get("title", "")).strip()
            next_step = str(args.get("next_step", "")).strip()
            if not title:
                raise ToolError("'title' is required.")
            if not next_step:
                raise ToolError(
                    "'next_step' is required — a proposal without a concrete first"
                    " step isn't actionable."
                )
            venture_id = await self.memory.add_venture(
                {
                    "title": title,
                    "thesis": str(args.get("thesis", "")),
                    "category": str(args.get("category", "other")),
                    "effort": str(args.get("effort", "medium")),
                    "horizon": str(args.get("horizon", "weeks")),
                    "confidence": float(args.get("confidence", 0.4) or 0.4),
                    "next_step": next_step,
                    "evidence": str(args.get("evidence", "")),
                }
            )
            return f"Venture #{venture_id} recorded: {title}"

        async def list_ventures(args: dict[str, Any]) -> Any:
            status = args.get("status")
            rows = await self.memory.list_ventures(
                None if status in (None, "all") else str(status)
            )
            return [
                {
                    "id": r["id"], "title": r["title"], "status": r["status"],
                    "confidence": r["confidence"], "effort": r["effort"],
                    "horizon": r["horizon"], "thesis": r["thesis"][:300],
                    "next_step": r["next_step"], "evidence": r["evidence"][:300],
                }
                for r in rows
            ]

        async def update_venture(args: dict[str, Any]) -> str:
            venture_id = int(args.get("venture_id", 0))
            ok = await self.memory.update_venture(
                venture_id,
                status=args.get("status"),
                next_step=args.get("next_step"),
                evidence=args.get("evidence"),
            )
            return f"Venture #{venture_id} updated." if ok else f"No venture #{venture_id}."

        self.add(
            Tool(
                name="propose_venture",
                description=(
                    "Record a concrete way the user could make money, grounded in"
                    " their actual skills, assets and time. Requires a real first"
                    " step they could take this week. Set confidence honestly —"
                    " most ideas deserve below 0.5."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "thesis": {
                            "type": "string",
                            "description": "Why this could work for them specifically,"
                                           " and what would have to be true.",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["service", "product", "content", "consulting",
                                     "arbitrage", "employment", "other"],
                        },
                        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                        "horizon": {
                            "type": "string",
                            "enum": ["days", "weeks", "months", "years"],
                        },
                        "confidence": {"type": "number"},
                        "next_step": {
                            "type": "string",
                            "description": "One concrete action for this week.",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "What you actually verified, and what you"
                                           " could not.",
                        },
                    },
                    "required": ["title", "next_step"],
                },
                handler=propose_venture,
            )
        )
        self.add(
            Tool(
                name="list_ventures",
                description="List tracked money-making opportunities and their status.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["proposed", "active", "parked", "dropped", "all"],
                        }
                    },
                },
                handler=list_ventures,
            )
        )
        self.add(
            Tool(
                name="update_venture",
                description=(
                    "Advance, park or drop a venture, or sharpen its next step as"
                    " you learn more."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "venture_id": {"type": "integer"},
                        "status": {
                            "type": "string",
                            "enum": ["proposed", "active", "parked", "dropped"],
                        },
                        "next_step": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["venture_id"],
                },
                handler=update_venture,
            )
        )


def _is_url(target: str) -> bool:
    return target.lower().startswith(URL_SCHEMES)


def _os_open(target: str) -> tuple[bool, str]:
    """Hand something to the desktop's default handler.

    Always an argv list, never a shell string, so a filename containing a
    space or a semicolon opens a file instead of running a command.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", target], capture_output=True, timeout=15,
                           check=True)
            return True, ""
        if system == "Windows":
            os.startfile(target)  # type: ignore[attr-defined]
            return True, ""
        if shutil.which("xdg-open"):
            subprocess.Popen(
                ["xdg-open", target],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True, ""
        return False, "no xdg-open on this system"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _launch_app(name: str) -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", "-a", name], capture_output=True, timeout=15,
                           check=True)
            return True, ""
        if system == "Windows":
            # `start` resolves registered app names as well as executables.
            subprocess.run(["cmd", "/c", "start", "", name], capture_output=True,
                           timeout=15, check=True)
            return True, ""
        binary = shutil.which(name) or shutil.which(name.lower())
        if not binary:
            return False, f"{name} is not on PATH"
        subprocess.Popen(
            [binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


# Virtual key codes for the media keys on a standard keyboard.
_WIN_MEDIA_KEYS = {
    "playpause": 0xB3, "play": 0xB3, "pause": 0xB3,
    "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
}


def _media_key(action: str) -> tuple[bool, str]:
    """Control playback without knowing which app is playing.

    Each platform has one blessed way to do this: media keys on Windows,
    MPRIS on Linux, and AppleScript against the frontmost player on macOS.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            # Key code 16 is F16/play on the media layer; Music and Spotify
            # both honour the system-wide play/pause event.
            mapping = {
                "playpause": 'tell application "System Events" to key code 49',
                "play": 'tell application "Music" to play',
                "pause": 'tell application "Music" to pause',
                "stop": 'tell application "Music" to stop',
                "next": 'tell application "Music" to next track',
                "previous": 'tell application "Music" to previous track',
            }
            subprocess.run(["osascript", "-e", mapping[action]],
                           capture_output=True, timeout=10)
            return True, ""
        if system == "Windows":
            import ctypes

            code = _WIN_MEDIA_KEYS[action]
            ctypes.windll.user32.keybd_event(code, 0, 0, 0)  # type: ignore[attr-defined]
            ctypes.windll.user32.keybd_event(code, 0, 2, 0)  # type: ignore[attr-defined]
            return True, ""
        if shutil.which("playerctl"):
            verb = {"playpause": "play-pause", "previous": "previous"}.get(
                action, action
            )
            subprocess.run(["playerctl", verb], capture_output=True, timeout=10)
            return True, ""
        return False, "install playerctl to control playback on Linux"
    except (OSError, subprocess.SubprocessError, KeyError) as exc:
        return False, str(exc)


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
