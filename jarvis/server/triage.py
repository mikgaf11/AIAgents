"""Autonomous email triage.

New mail is classified against *your* priorities, not a generic notion of
importance: the classifier is handed your stored facts and open goals, so
a message about a project you're actually working on outranks a louder
one that has nothing to do with you.

Results are written back into the real mailbox as IMAP flags, so the
triage is visible in whatever mail client you already use — JARVIS isn't
a place you have to go and check.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .config import CONFIG
from .connectors.email import EmailConnector, EmailError, EmailMessage
from .events import BUS
from .memory import PRIORITY_LABELS, Memory

TRIAGE_SYSTEM = """\
You triage incoming email for one person. You are given what is known about \
them — their stored facts, their open goals, the ventures they are pursuing — \
and a batch of new messages. Judge importance *to this person*, not in the \
abstract.

Return a JSON array with one object per message, in the order given, and \
nothing else — no prose, no code fence:

[{"uid": 123,
  "priority": 0-4,
  "category": "personal|work|financial|opportunity|admin|newsletter|automated|spam",
  "summary": "one clause on what it actually says",
  "action": "the single next action, or empty string if none",
  "needs_reply": true|false}]

Priority means:
  4 critical  - real deadline today, money at genuine risk, an emergency, a
                person who matters waiting on them right now
  3 high      - matters to a stated goal or venture, or a real person expecting
                a reply in the next day or so
  2 normal    - legitimate mail worth reading, no urgency
  1 low       - receipts, notifications, things to skim eventually
  0 noise     - marketing, cold outreach, automated churn, anything safely ignored

Be strict about the top of the scale. If everything is critical, nothing is: \
reserve 4 for messages where a delay of hours causes real harm, and expect \
most mail to land at 1 or 0. Marketing that calls itself urgent is still 0. \
Never raise priority because the sender used the word urgent.

Write the summary in plain words a person would use out loud. Leave action \
empty rather than inventing busywork.\
"""


class TriageEngine:
    def __init__(self, memory: Memory, connector: EmailConnector | None, client) -> None:
        self.memory = memory
        self.connector = connector
        self.client = client
        self.last_error: str | None = None
        self.last_sweep: float = 0.0
        self.swept = 0

    @property
    def enabled(self) -> bool:
        return bool(self.connector and self.connector.configured and self.client)

    # -- the sweep -------------------------------------------------------

    async def sweep(self) -> list[dict[str, Any]]:
        """Fetch and triage anything new. Returns the newly triaged records."""
        if not self.enabled:
            return []

        state_key = f"email:{self.connector.address}:last_uid"
        try:
            since_uid = int(await self.memory.get_state(state_key, "0") or 0)
        except ValueError:
            since_uid = 0

        try:
            messages = await self.connector.fetch_new(since_uid, CONFIG.email_batch)
        except EmailError as exc:
            # Don't spam the HUD with the same failure every poll.
            if str(exc) != self.last_error:
                self.last_error = str(exc)
                BUS.emit("error", message=f"Email: {exc}")
                BUS.emit("email_status", ok=False, detail=str(exc))
            return []

        self.last_error = None
        self.last_sweep = time.time()
        if not messages:
            BUS.emit("email_status", ok=True, detail="no new mail", checked=self.last_sweep)
            return []

        BUS.emit("email_status", ok=True, detail=f"triaging {len(messages)}",
                 checked=self.last_sweep)

        verdicts = await self._classify(messages)
        records: list[dict[str, Any]] = []

        for message in messages:
            verdict = verdicts.get(message.uid, {})
            record = {
                "account": self.connector.address,
                "folder": message.folder,
                "uid": message.uid,
                "message_id": message.message_id,
                "sender": message.sender,
                "sender_email": message.sender_email,
                "subject": message.subject,
                "snippet": message.snippet,
                "received": message.received,
                "priority": _clamp_priority(verdict.get("priority", 2)),
                "category": str(verdict.get("category", "other"))[:40],
                "summary": str(verdict.get("summary", ""))[:400],
                "action": str(verdict.get("action", ""))[:300],
                "needs_reply": bool(verdict.get("needs_reply")),
            }
            row_id = await self.memory.record_email(record)
            if row_id is None:
                continue  # already triaged in an earlier sweep
            record["id"] = row_id
            record["priority_label"] = PRIORITY_LABELS[record["priority"]]
            records.append(record)

            BUS.emit(
                "email",
                id=row_id,
                sender=record["sender"],
                subject=record["subject"],
                priority=record["priority"],
                label=record["priority_label"],
                category=record["category"],
                summary=record["summary"],
                action=record["action"],
                needs_reply=record["needs_reply"],
            )

            # Mirror the judgement into the actual mailbox.
            if CONFIG.email_flag_important and record["priority"] >= 3:
                await self.connector.set_flags(message.uid, "\\Flagged")

        # Advance past everything fetched, including messages that turned out
        # to be duplicates — otherwise the cursor sticks and we re-fetch them
        # on every sweep forever.
        await self.memory.set_state(state_key, str(max(m.uid for m in messages)))
        self.swept += len(records)
        return records

    # -- classification --------------------------------------------------

    async def _classify(self, messages: list[EmailMessage]) -> dict[int, dict[str, Any]]:
        """One model call for the whole batch — cheaper and more consistent."""
        context = await self._user_context()
        batch = "\n\n---\n\n".join(m.for_triage() for m in messages)

        try:
            response = await self.client.messages.create(
                model=CONFIG.background_model,
                max_tokens=CONFIG.background_max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": TRIAGE_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config={"effort": CONFIG.background_effort},
                messages=[
                    {
                        "role": "user",
                        "content": f"What is known about them:\n{context}\n\n"
                                   f"New messages:\n\n{batch}",
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - triage is best-effort
            BUS.emit("error", message=f"Triage call failed: {exc}")
            return {}

        if getattr(response, "stop_reason", None) == "refusal":
            return {}

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        parsed = _parse_json_array(text)
        verdicts: dict[int, dict[str, Any]] = {}
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                continue
            # Trust the uid when it's valid, fall back to position.
            uid = item.get("uid")
            if not isinstance(uid, int) or uid not in {m.uid for m in messages}:
                uid = messages[index].uid if index < len(messages) else None
            if uid is not None:
                verdicts[uid] = item
        return verdicts

    async def _user_context(self) -> str:
        facts = await self.memory.all_facts(25)
        goals = await self.memory.list_goals("open")
        ventures = await self.memory.list_ventures("active")
        lines = []
        for fact in facts:
            lines.append(f"- {fact['subject']}: {fact['content'][:160]}")
        for goal in goals:
            lines.append(f"- goal: {goal['title']}")
        for venture in ventures:
            lines.append(f"- venture: {venture['title']}")
        return "\n".join(lines) or "- (nothing stored about them yet)"

    # -- status ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.connector and self.connector.configured),
            "enabled": self.enabled,
            "address": self.connector.address if self.connector else "",
            "provider": self.connector.provider if self.connector else None,
            "last_sweep": self.last_sweep,
            "triaged": self.swept,
            "error": self.last_error,
            "can_send": CONFIG.email_allow_send,
        }


def _clamp_priority(value: Any) -> int:
    try:
        return max(0, min(4, int(value)))
    except (TypeError, ValueError):
        return 2


def _parse_json_array(text: str) -> list[Any]:
    """Pull a JSON array out of model output, tolerating fences and prose."""
    if not text:
        return []
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
