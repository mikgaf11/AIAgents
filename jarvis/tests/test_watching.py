"""Watching what the user does, and deciding whether to say anything.

The interesting cases here are all about restraint: sessions short enough to
be alt-tab noise are discarded, nudges are rate limited, and the playtime
warning fires once rather than every tick.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.activity import ActivityMonitor, Sample  # noqa: E402
from server.autonomy import Autonomy  # noqa: E402
from server.config import CONFIG  # noqa: E402
from server.events import BUS  # noqa: E402
from server.learning import Learning, _parse_json_object  # noqa: E402
from server.memory import Memory  # noqa: E402
from server.notify import Notifier  # noqa: E402


@pytest.fixture()
def memory(tmp_path):
    store = Memory(tmp_path / "watch.db")
    yield store
    store.close()


@pytest.fixture()
def settings():
    """Override frozen Config fields for one test, then put them back."""
    saved: dict[str, object] = {}

    def override(**values):
        for key, value in values.items():
            saved.setdefault(key, getattr(CONFIG, key))
            object.__setattr__(CONFIG, key, value)

    yield override
    for key, value in saved.items():
        object.__setattr__(CONFIG, key, value)


@pytest.fixture()
def monitor(memory):
    return ActivityMonitor(memory)


# -- classification --------------------------------------------------------


def test_apps_are_sorted_into_categories(monitor):
    assert monitor.classify("Steam", "Counter-Strike 2") == "game"
    assert monitor.classify("Code.exe", "tools.py — jarvis") == "code"
    assert monitor.classify("Spotify", "Miles Davis") == "media"
    assert monitor.classify("Slack", "#general") == "comms"


def test_a_browser_is_classified_by_what_it_is_showing(monitor):
    # The app name alone says nothing useful — the tab is the activity.
    assert monitor.classify("chrome", "YouTube - some video") == "media"
    assert monitor.classify("chrome", "github.com/anthropics") == "code"
    assert monitor.classify("chrome", "Quarterly plan - docs.google.com") == "work"
    assert monitor.classify("chrome", "New Tab") == "browse"


def test_unknown_apps_fall_through_to_other(monitor):
    assert monitor.classify("SomeInternalTool", "widget") == "other"


def test_custom_rules_extend_the_defaults(memory, tmp_path, monkeypatch):
    rules = CONFIG.db_path.parent / "activity_rules.json"
    original = rules.read_text() if rules.exists() else None
    rules.write_text(json.dumps({"game": ["mygameclient"]}), encoding="utf-8")
    try:
        custom = ActivityMonitor(memory)
        assert custom.classify("MyGameClient", "") == "game"
        # Defaults survive alongside the override.
        assert custom.classify("Spotify", "") == "media"
    finally:
        if original is None:
            rules.unlink()
        else:
            rules.write_text(original, encoding="utf-8")


def test_a_broken_rules_file_is_ignored_rather_than_fatal(memory):
    rules = CONFIG.db_path.parent / "activity_rules.json"
    original = rules.read_text() if rules.exists() else None
    rules.write_text("{not json", encoding="utf-8")
    try:
        assert ActivityMonitor(memory).classify("Steam", "") == "game"
    finally:
        if original is None:
            rules.unlink()
        else:
            rules.write_text(original, encoding="utf-8")


# -- sessions --------------------------------------------------------------


def test_alt_tab_noise_is_not_recorded(monitor, memory):
    async def run():
        monitor.current = Sample("Chrome", "tab", "browse", time.time())
        # Well under activity_min_session.
        monitor.session_started = time.time() - 3
        await monitor._close_session()
        assert await memory.recent_activity(10) == []

    asyncio.run(run())


def test_a_real_stretch_is_recorded_with_its_duration(monitor, memory):
    async def run():
        started = time.time() - 900
        monitor.current = Sample("Steam", "Elden Ring", "game", time.time())
        monitor.session_started = started
        await monitor._close_session()

        rows = await memory.recent_activity(10)
        assert len(rows) == 1
        assert rows[0]["app"] == "Steam"
        assert rows[0]["category"] == "game"
        assert 890 < rows[0]["seconds"] < 910
        # The session is closed, not left dangling.
        assert monitor.current is None

    asyncio.run(run())


def test_switching_apps_closes_the_previous_session(monitor, memory, monkeypatch):
    async def run():
        monkeypatch.setattr(
            "server.activity.foreground", lambda: ("Steam", "Elden Ring")
        )
        await monitor.sample()
        monitor.session_started = time.time() - 600  # pretend it ran a while

        monkeypatch.setattr("server.activity.foreground", lambda: ("Code", "main.py"))
        await monitor.sample()

        rows = await memory.recent_activity(10)
        assert [r["app"] for r in rows] == ["Steam"]
        assert monitor.current.app == "Code"

    asyncio.run(run())


def test_today_includes_the_session_still_in_progress(monitor, memory):
    async def run():
        monitor.current = Sample("Steam", "game", "game", time.time())
        monitor.session_started = time.time() - 1800
        totals = await monitor.today()
        assert 29 < totals["game"] < 31

    asyncio.run(run())


def test_play_minutes_sums_only_the_play_categories(monitor, memory):
    async def run():
        now = time.time()
        await memory.record_activity(
            app="Steam", title="", category="game", started=now - 3600, seconds=3600
        )
        await memory.record_activity(
            app="Code", title="", category="code", started=now - 1800, seconds=1800
        )
        assert round(await monitor.play_minutes()) == 60

    asyncio.run(run())


# -- rate limiting ---------------------------------------------------------


def test_nudges_respect_the_minimum_gap(settings):
    settings(nudge_min_gap=900.0)
    notifier = Notifier()
    notifier.last_nudge = time.time()
    assert notifier.may_nudge() is False
    # Something genuinely urgent still gets through.
    assert notifier.may_nudge(urgent=True) is True


def test_nudges_are_capped_per_hour(settings):
    settings(nudge_min_gap=0.0, nudge_max_per_hour=3)
    notifier = Notifier()
    now = time.time()
    notifier.recent.extend([now - 10, now - 20, now - 30])
    assert notifier.may_nudge() is False
    # An hour later those no longer count.
    notifier.recent.clear()
    notifier.recent.extend([now - 4000, now - 5000])
    assert notifier.may_nudge() is True


def test_snoozing_silences_everything_but_the_urgent():
    notifier = Notifier()
    notifier.snooze(30)
    assert notifier.may_nudge() is False
    assert notifier.may_nudge(urgent=True) is True


def test_a_suppressed_nudge_reports_that_it_did_not_deliver(settings):
    settings(desktop_notifications=False)

    async def run():
        notifier = Notifier()
        notifier.snooze(60)
        assert await notifier.nudge("hello") is False
        assert notifier.delivered == 0

    asyncio.run(run())


def test_an_empty_nudge_is_never_delivered():
    async def run():
        assert await Notifier().nudge("   ") is False

    asyncio.run(run())


def test_a_delivered_nudge_reaches_the_bus_intact(settings):
    """The whole point of a nudge is that it arrives, so exercise the bus.

    The payload cannot use the key 'kind' — that is the event's own name on
    the bus, and the collision raises a TypeError at emit time rather than
    anywhere near here.
    """
    settings(desktop_notifications=False, nudge_min_gap=0.0)

    async def run():
        queue = BUS.subscribe()
        try:
            assert await Notifier().nudge("Stand up.", kind="playtime") is True
            events = []
            while not queue.empty():
                events.append(queue.get_nowait())
            nudges = [e for e in events if e["kind"] == "nudge"]
            assert len(nudges) == 1
            assert nudges[0]["text"] == "Stand up."
            assert nudges[0]["tone"] == "playtime"
        finally:
            BUS.unsubscribe(queue)

    asyncio.run(run())


# -- the playtime routine --------------------------------------------------


class _FakeJarvis:
    def __init__(self, memory):
        self.memory = memory
        self.online = True
        self.busy = asyncio.Lock()
        self.client = None
        self.tools = None
        self.model = "test-model"
        self.background_model = "test-model"


class _FakeActivity:
    def __init__(self, minutes: float, app: str = "Steam"):
        self.minutes = minutes
        self.app = app

    async def play_minutes(self) -> float:
        return self.minutes

    async def today(self) -> dict[str, float]:
        return {"game": self.minutes}

    def status(self):
        return {"current": {"app": self.app, "category": "game",
                            "minutes": self.minutes, "title": ""}}


def test_playtime_says_nothing_below_the_limit(memory, monkeypatch):
    async def run():
        sent = []
        monkeypatch.setattr(
            "server.autonomy.NOTIFIER.nudge",
            lambda text, **kw: _record(sent, text),
        )
        autonomy = Autonomy(_FakeJarvis(memory), _FakeTriage(),
                            activity=_FakeActivity(30))
        await autonomy.run_playtime_check()
        assert sent == []

    asyncio.run(run())


def test_playtime_warns_once_and_then_holds_off(memory, monkeypatch, settings):
    settings(play_limit_minutes=60.0, play_reminder_every=45.0)

    async def run():
        sent = []
        monkeypatch.setattr(
            "server.autonomy.NOTIFIER.nudge",
            lambda text, **kw: _record(sent, text),
        )
        autonomy = Autonomy(_FakeJarvis(memory), _FakeTriage(),
                            activity=_FakeActivity(180))
        await autonomy.run_playtime_check()
        assert len(sent) == 1
        assert "3.0 hours" in sent[0]

        # The next tick a minute later must not repeat it.
        await autonomy.run_playtime_check()
        assert len(sent) == 1

    asyncio.run(run())


def test_dropping_back_under_the_limit_rearms_the_warning(memory, monkeypatch, settings):
    settings(play_limit_minutes=60.0)

    async def run():
        sent = []
        monkeypatch.setattr(
            "server.autonomy.NOTIFIER.nudge",
            lambda text, **kw: _record(sent, text),
        )
        activity = _FakeActivity(90)
        autonomy = Autonomy(_FakeJarvis(memory), _FakeTriage(), activity=activity)
        await autonomy.run_playtime_check()
        assert len(sent) == 1

        # A new day: the counter resets, so the warning must arm again.
        activity.minutes = 5
        await autonomy.run_playtime_check()
        assert autonomy.play_warned_at == 0.0

        activity.minutes = 120
        await autonomy.run_playtime_check()
        assert len(sent) == 2

    asyncio.run(run())


def test_routines_are_disabled_when_their_subsystem_is_missing(memory):
    autonomy = Autonomy(_FakeJarvis(memory), _FakeTriage())
    by_name = {r.name: r for r in autonomy.routines}
    assert by_name["playtime"].enabled is False
    assert by_name["coach"].enabled is False
    assert by_name["learning"].enabled is False


# -- learning --------------------------------------------------------------


def test_mining_does_nothing_when_nothing_has_been_observed(memory):
    async def run():
        jarvis = _FakeJarvis(memory)
        learning = Learning(jarvis, _FakeActivity(0))
        assert await learning.mine() == []

    asyncio.run(run())


def test_mined_insights_and_facts_are_written(memory, monkeypatch):
    async def run():
        jarvis = _FakeJarvis(memory)
        learning = Learning(jarvis, _FakeActivity(0))
        await memory.add_episode("user", "help me ship the launch page")

        async def fake_ask(system, content, *, max_tokens):
            return {
                "insights": [
                    {"topic": "work.peak_hours",
                     "insight": "They code late, between 9pm and 1am.",
                     "evidence": "six sessions after 21:00 this week",
                     "confidence": 0.7},
                    {"topic": "", "insight": "dropped — no topic"},
                ],
                "facts": [{"subject": "editor", "content": "VS Code",
                           "importance": 0.6}],
            }

        monkeypatch.setattr(learning, "_ask", fake_ask)
        written = await learning.mine()

        assert [w["topic"] for w in written] == ["work.peak_hours"]
        insights = await memory.list_insights(10)
        assert insights[0]["insight"].startswith("They code late")
        assert any(f["subject"] == "editor" for f in await memory.all_facts(10))

    asyncio.run(run())


def test_confidence_outside_zero_to_one_is_clamped(memory, monkeypatch):
    async def run():
        learning = Learning(_FakeJarvis(memory), _FakeActivity(0))
        await memory.add_episode("user", "anything")

        async def fake_ask(system, content, *, max_tokens):
            return {"insights": [
                {"topic": "a", "insight": "over", "confidence": 4.2},
                {"topic": "b", "insight": "nonsense", "confidence": "very"},
            ]}

        monkeypatch.setattr(learning, "_ask", fake_ask)
        await learning.mine()
        by_topic = {i["topic"]: i for i in await memory.list_insights(10)}
        assert by_topic["a"]["confidence"] == 1.0
        assert 0.0 <= by_topic["b"]["confidence"] <= 1.0

    asyncio.run(run())


def test_coaching_stays_silent_when_the_model_declines(memory, monkeypatch):
    async def run():
        learning = Learning(_FakeJarvis(memory), _FakeActivity(20))

        async def fake_ask(system, content, *, max_tokens):
            return {"speak": None, "reason": "nothing worth saying"}

        monkeypatch.setattr(learning, "_ask", fake_ask)
        assert await learning.coach() is None

    asyncio.run(run())


def test_coaching_remembers_its_last_topic_so_it_does_not_repeat(memory, monkeypatch):
    async def run():
        learning = Learning(_FakeJarvis(memory), _FakeActivity(200))

        async def fake_ask(system, content, *, max_tokens):
            # The prompt must carry the previous topic forward.
            fake_ask.saw = content
            return {"speak": "Long stretch. Stand up.", "reason": "posture"}

        monkeypatch.setattr(learning, "_ask", fake_ask)
        assert await learning.coach() == "Long stretch. Stand up."
        assert learning.last_coach_topic == "posture"

        await learning.coach()
        assert "posture" in fake_ask.saw

    asyncio.run(run())


def test_coaching_says_nothing_while_a_conversation_is_live(memory, monkeypatch):
    async def run():
        jarvis = _FakeJarvis(memory)
        learning = Learning(jarvis, _FakeActivity(300))
        async with jarvis.busy:
            assert await learning.coach() is None

    asyncio.run(run())


def test_json_is_recovered_from_prose_and_fences():
    assert _parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json_object('Sure!\n{"a": 2}\nHope that helps.') == {"a": 2}
    assert _parse_json_object("no json here") is None
    assert _parse_json_object('[1, 2]') is None  # an array is not an object


# -- helpers ---------------------------------------------------------------


class _FakeTriage:
    enabled = False

    def status(self):
        return {"configured": False}


async def _record(sink, text):
    sink.append(text)
    return True
