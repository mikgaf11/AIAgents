"""Getting better at knowing you.

Two loops, and it's worth being precise about what each does, because
"learning" gets used loosely.

`mine()` looks at what actually happened — where your hours went, when you
work, what you asked for, what you corrected — and writes durable
*insights*. Those insights are fed back into every future conversation as
context, so the assistant's answers improve as the file grows. This is
retrieval getting richer, not weights changing.

`coach()` decides whether right now is a moment worth saying something
about, and what. It is deliberately conservative: it looks for a concrete
observation with evidence behind it, and stays quiet otherwise.

Nothing here fine-tunes a model. That would need GPUs and a curated
dataset, and on personal data it reliably makes a model worse rather than
better. Accumulated context is what actually makes an assistant feel like
it knows you.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .config import CONFIG
from .events import BUS

MINING_PROMPT = f"""\
You study how one person actually spends their time, so that
{CONFIG.assistant_name} can be more useful to them. You are given their
recent activity (application and window title only), what they have asked
for, notes from earlier passes, and any corrections they made.

Return a JSON object and nothing else — no prose, no code fence:

{{
  "insights": [
    {{"topic": "short stable key, e.g. work.peak_hours",
      "insight": "one sentence, specific and useful",
      "evidence": "what in the data supports it",
      "confidence": 0.0}}
  ],
  "facts": [{{"subject": "...", "content": "...", "importance": 0.0}}]
}}

What makes an insight worth recording:

It has to be *actionable or predictive* — something that would change how
you'd answer them tomorrow. "They code most between 9pm and 1am" is useful.
"They used a web browser" is not.

Ground every one in the data you were given, and say so in evidence. Do not
invent patterns from a single sample: if you only have a few hours of
history, say so with low confidence rather than manufacturing certainty.
Prefer updating an existing topic over inventing a near-duplicate.

Use "facts" only for things that are stable and near-certain — a timezone, a
tool they clearly live in, a recurring commitment. Everything speculative
belongs in insights with an honest confidence.

An empty list is a perfectly good answer on thin data.\
"""

COACHING_PROMPT = f"""\
You are {CONFIG.assistant_name}, deciding whether to interrupt.

You are given what they are doing right now, how long they have been doing
it, where today's hours have gone, their goals, and what you already know
about them.

Return JSON and nothing else:

{{"speak": null, "kind": "coach", "reason": "why, in a few words"}}

Set "speak" to one or two spoken sentences ONLY when there is a genuinely
useful thing to say right now. Good reasons: they have been at something
far longer than is good for them, they are drifting from a goal they told
you mattered, a pattern you have evidence for is repeating unhelpfully, or
there is a concrete suggestion that would help *this* moment.

Silence is the default and by far the most common correct answer. Do not
speak to check in, encourage, praise, greet, or narrate what they are
doing. Do not moralise about how they spend their time, and never nag about
the same thing twice in a row.

Write it as a person would say it out loud: direct, warm, brief, no
markdown, no lists. Say the useful thing and stop.\
"""


class Learning:
    """Mines patterns and decides when coaching is warranted."""

    def __init__(self, jarvis, activity) -> None:
        self.jarvis = jarvis
        self.memory = jarvis.memory
        self.activity = activity
        self.mined = 0
        self.last_coach_topic = ""

    # -- pattern mining --------------------------------------------------

    async def mine(self) -> list[dict[str, Any]]:
        """One pass over recent behaviour, writing what it concludes."""
        if not (CONFIG.learning_enabled and self.jarvis.online):
            return []

        day = 86400.0
        breakdown = await self.memory.activity_breakdown(time.time() - 7 * day, 20)
        recent = await self.memory.recent_activity(60)
        episodes = await self.memory.recent_episodes(25)
        insights = await self.memory.list_insights(20)
        feedback = await self.memory.feedback_summary(20)
        goals = await self.memory.list_goals("open")

        if not breakdown and not episodes:
            return []  # nothing observed yet

        def block(title: str, rows: list[str]) -> str:
            return f"{title}:\n" + ("\n".join(f"  {r}" for r in rows) or "  (none)")

        snapshot = "\n\n".join([
            block("Where the last week went (minutes per app)", [
                f'{b["app"]} [{b["category"]}] {b["minutes"]}m over {b["sessions"]} sessions'
                for b in breakdown
            ]),
            block("Most recent sessions", [
                f'{time.strftime("%a %H:%M", time.localtime(a["started"]))} '
                f'{a["app"]} [{a["category"]}] {round(a["seconds"] / 60)}m'
                for a in recent[:25]
            ]),
            block("What they have been asking for", [
                f'{e["role"]}: {e["content"][:160]}' for e in episodes
            ]),
            block("Their stated goals", [g["title"] for g in goals]),
            block("What you already concluded (update rather than repeat)", [
                f'{i["topic"]}: {i["insight"]} (confidence {i["confidence"]:.2f})'
                for i in insights
            ]),
            block("Corrections and reactions from them", [
                f'{f["signal"]} — {f["subject"]}: {f["detail"][:120]}' for f in feedback
            ]),
        ])

        verdict = await self._ask(MINING_PROMPT, snapshot, max_tokens=2500)
        if not verdict:
            return []

        written: list[dict[str, Any]] = []
        for item in verdict.get("insights") or []:
            if not isinstance(item, dict):
                continue
            topic = str(item.get("topic", "")).strip()
            insight = str(item.get("insight", "")).strip()
            if not topic or not insight:
                continue
            await self.memory.add_insight(
                topic, insight,
                evidence=str(item.get("evidence", ""))[:500],
                confidence=_clamp(item.get("confidence", 0.4)),
            )
            written.append({"topic": topic, "insight": insight})
            BUS.emit("insight", topic=topic, text=insight,
                     confidence=_clamp(item.get("confidence", 0.4)))

        for fact in verdict.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            subject = str(fact.get("subject", "")).strip()
            content = str(fact.get("content", "")).strip()
            if subject and content:
                await self.memory.add_fact(
                    subject, content,
                    importance=_clamp(fact.get("importance", 0.5)),
                    source="learned",
                )

        self.mined += 1
        return written

    # -- coaching --------------------------------------------------------

    async def coach(self) -> str | None:
        """Decide whether to say something right now. Usually: no."""
        if not (CONFIG.coaching_enabled and self.jarvis.online):
            return None
        if self.jarvis.busy.locked():
            return None

        status = self.activity.status()
        current = status.get("current") or {}
        if not current.get("app"):
            return None

        totals = await self.activity.today()
        insights = await self.memory.list_insights(12)
        goals = await self.memory.list_goals("open")

        snapshot = "\n".join([
            f"Right now: {current['app']} ({current['category']}) "
            f"for {current['minutes']} minutes — {current['title']}",
            "",
            "Today so far: " + (
                ", ".join(f"{k} {v}m" for k, v in sorted(
                    totals.items(), key=lambda kv: -kv[1])) or "nothing recorded"
            ),
            "",
            "Their goals: " + (", ".join(g["title"] for g in goals) or "none set"),
            "",
            "What you know about them:",
            *(f"  {i['topic']}: {i['insight']}" for i in insights),
            "",
            f"The last thing you nudged about was: {self.last_coach_topic or 'nothing'}."
            " Do not repeat it.",
        ])

        verdict = await self._ask(COACHING_PROMPT, snapshot, max_tokens=700)
        if not verdict:
            return None
        speak = verdict.get("speak")
        if not speak or not str(speak).strip():
            return None
        self.last_coach_topic = str(verdict.get("reason", ""))[:120]
        return str(speak).strip()

    # -- helper ----------------------------------------------------------

    async def _ask(self, system: str, content: str, *, max_tokens: int) -> dict | None:
        try:
            response = await self.jarvis.client.messages.create(
                model=self.jarvis.background_model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": CONFIG.background_effort},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - learning is best-effort
            BUS.emit("error", message=f"Learning call failed: {exc}")
            return None
        if getattr(response, "stop_reason", None) == "refusal":
            return None
        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        )
        return _parse_json_object(text)

    def status(self) -> dict[str, Any]:
        return {"enabled": CONFIG.learning_enabled, "passes": self.mined}


def _clamp(value: Any, default: float = 0.4) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _parse_json_object(text: str) -> dict | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
