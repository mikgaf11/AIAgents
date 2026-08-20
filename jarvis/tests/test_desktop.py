"""The tools that reach out onto the actual machine.

Nothing here launches a real application — the OS calls are stubbed. What
is worth testing is the boundary: which paths are reachable, that a
filename is never handed to a shell, and that a disabled capability says so
instead of failing obscurely.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import CONFIG  # noqa: E402
from server.memory import Memory  # noqa: E402
from server import tools as tools_module  # noqa: E402
from server.tools import ToolRegistry, _is_url  # noqa: E402


@pytest.fixture()
def memory(tmp_path):
    store = Memory(tmp_path / "desktop.db")
    yield store
    store.close()


@pytest.fixture()
def tools(memory):
    return ToolRegistry(memory)


@pytest.fixture()
def settings():
    saved: dict[str, object] = {}

    def override(**values):
        for key, value in values.items():
            saved.setdefault(key, getattr(CONFIG, key))
            object.__setattr__(CONFIG, key, value)

    yield override
    for key, value in saved.items():
        object.__setattr__(CONFIG, key, value)


@pytest.fixture()
def opened(monkeypatch):
    """Capture whatever would have been handed to the desktop."""
    calls: list[str] = []
    monkeypatch.setattr(
        tools_module, "_os_open", lambda target: (calls.append(target), (True, ""))[1]
    )
    monkeypatch.setattr(
        tools_module, "_launch_app", lambda name: (calls.append(name), (True, ""))[1]
    )
    return calls


# -- reachable paths -------------------------------------------------------


def test_extra_roots_widen_access_without_disabling_safe_mode(tools, settings, tmp_path):
    async def run():
        secret = tmp_path / "notes.txt"
        secret.write_text("the plan", encoding="utf-8")

        # Outside the workspace, so refused by default.
        output, is_error = await tools.execute("read_file", {"path": str(secret)})
        assert is_error and "JARVIS_FILE_ROOTS" in output

        settings(file_roots=(str(tmp_path),))
        output, is_error = await tools.execute("read_file", {"path": str(secret)})
        assert not is_error and output == "the plan"

    asyncio.run(run())


def test_a_granted_root_does_not_grant_its_siblings(tools, settings, tmp_path):
    async def run():
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        sibling = tmp_path / "private"
        sibling.mkdir()
        (sibling / "keys.txt").write_text("secret", encoding="utf-8")

        settings(file_roots=(str(allowed),))
        _, is_error = await tools.execute(
            "read_file", {"path": str(sibling / "keys.txt")}
        )
        assert is_error

    asyncio.run(run())


def test_traversal_out_of_an_allowed_root_is_refused(tools, settings, tmp_path):
    async def run():
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
        settings(file_roots=(str(allowed),))

        _, is_error = await tools.execute(
            "read_file", {"path": str(allowed / ".." / "outside.txt")}
        )
        assert is_error

    asyncio.run(run())


# -- finding files ---------------------------------------------------------


def test_find_files_matches_on_part_of_a_name(tools, settings, tmp_path):
    async def run():
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "tax-return-2025.pdf").write_bytes(b"x")
        (tmp_path / "unrelated.txt").write_bytes(b"y")
        settings(file_roots=(str(tmp_path),))

        output, is_error = await tools.execute("find_files", {"name": "tax-return"})
        assert not is_error
        assert "tax-return-2025.pdf" in output
        assert "unrelated.txt" not in output

    asyncio.run(run())


def test_find_files_skips_the_caches_that_make_a_walk_slow(tools, settings, tmp_path):
    async def run():
        noisy = tmp_path / "node_modules" / "pkg"
        noisy.mkdir(parents=True)
        (noisy / "target.js").write_bytes(b"x")
        (tmp_path / "target.js").write_bytes(b"x")
        settings(file_roots=(str(tmp_path),))

        output, _ = await tools.execute("find_files", {"name": "target.js"})
        assert "node_modules" not in output
        assert output.count("target.js") == 1

    asyncio.run(run())


def test_find_files_reports_an_honest_miss(tools, settings, tmp_path):
    async def run():
        settings(file_roots=(str(tmp_path),))
        output, is_error = await tools.execute("find_files", {"name": "nothing-here"})
        assert not is_error
        assert "No file matching" in output

    asyncio.run(run())


def test_find_files_needs_a_name(tools):
    async def run():
        output, is_error = await tools.execute("find_files", {})
        assert is_error and "'name' is required" in output

    asyncio.run(run())


# -- opening things --------------------------------------------------------


def test_open_path_hands_the_resolved_path_to_the_desktop(tools, settings, tmp_path, opened):
    async def run():
        target = tmp_path / "report.pdf"
        target.write_bytes(b"x")
        settings(file_roots=(str(tmp_path),), allow_open=True)

        output, is_error = await tools.execute("open_path", {"target": str(target)})
        assert not is_error
        assert opened == [str(target)]
        assert "Opened" in output

    asyncio.run(run())


def test_a_filename_with_shell_characters_is_still_just_a_filename(
    tools, settings, tmp_path, opened
):
    """The whole reason _os_open takes an argv list rather than a string."""
    async def run():
        nasty = tmp_path / "report; rm -rf ~.pdf"
        nasty.write_bytes(b"x")
        settings(file_roots=(str(tmp_path),), allow_open=True)

        _, is_error = await tools.execute("open_path", {"target": str(nasty)})
        assert not is_error
        assert opened == [str(nasty)]

    asyncio.run(run())


def test_urls_are_passed_through_without_touching_the_filesystem(
    tools, settings, opened
):
    async def run():
        settings(allow_open=True)
        _, is_error = await tools.execute(
            "open_path", {"target": "https://example.com/a"}
        )
        assert not is_error
        assert opened == ["https://example.com/a"]

    asyncio.run(run())


def test_opening_something_that_does_not_exist_says_so(tools, settings, tmp_path, opened):
    async def run():
        settings(file_roots=(str(tmp_path),), allow_open=True)
        output, is_error = await tools.execute(
            "open_path", {"target": str(tmp_path / "ghost.txt")}
        )
        assert is_error and "Nothing at" in output
        assert opened == []

    asyncio.run(run())


def test_open_and_launch_report_clearly_when_disabled(tools, settings, opened):
    async def run():
        settings(allow_open=False)
        for name, args in (
            ("open_path", {"target": "https://example.com"}),
            ("launch_app", {"name": "Spotify"}),
            ("play_music", {"query": "https://example.com/song"}),
        ):
            output, is_error = await tools.execute(name, args)
            assert is_error and "JARVIS_ALLOW_OPEN" in output
        assert opened == []

    asyncio.run(run())


def test_app_names_may_not_smuggle_shell_metacharacters(tools, settings, opened):
    async def run():
        settings(allow_open=True)
        output, is_error = await tools.execute(
            "launch_app", {"name": "Spotify; curl evil.example"}
        )
        assert is_error and "metacharacters" in output
        assert opened == []

    asyncio.run(run())


# -- music -----------------------------------------------------------------


def test_play_music_finds_a_local_track(tools, settings, tmp_path, opened):
    async def run():
        library = tmp_path / "music"
        (library / "jazz").mkdir(parents=True)
        track = library / "jazz" / "Kind of Blue.mp3"
        track.write_bytes(b"x")
        (library / "notes.txt").write_bytes(b"x")  # not audio
        settings(music_dirs=(str(library),), allow_open=True)

        output, is_error = await tools.execute("play_music", {"query": "kind of blue"})
        assert not is_error
        assert opened == [str(track)]
        assert "Kind of Blue.mp3" in output

    asyncio.run(run())


def test_play_music_explains_what_to_do_when_the_library_is_empty(
    tools, settings, tmp_path, opened
):
    async def run():
        settings(music_dirs=(str(tmp_path),), allow_open=True)
        output, is_error = await tools.execute("play_music", {"query": "anything"})
        assert is_error and "JARVIS_MUSIC_DIRS" in output

    asyncio.run(run())


def test_media_control_rejects_an_action_it_cannot_perform(tools):
    async def run():
        output, is_error = await tools.execute("media_control", {"action": "explode"})
        assert is_error and "action must be" in output

    asyncio.run(run())


def test_media_control_reports_a_missing_helper_rather_than_pretending(
    tools, monkeypatch
):
    async def run():
        monkeypatch.setattr(
            tools_module, "_media_key",
            lambda action: (False, "install playerctl to control playback on Linux"),
        )
        output, is_error = await tools.execute("media_control", {"action": "next"})
        assert is_error and "playerctl" in output

    asyncio.run(run())


# -- notifications ---------------------------------------------------------


def test_notify_me_reports_when_the_rate_limiter_swallowed_it(tools, settings):
    async def run():
        from server.notify import NOTIFIER

        settings(desktop_notifications=False)
        NOTIFIER.snooze(60)
        try:
            output, is_error = await tools.execute("notify_me", {"text": "hello"})
            assert not is_error
            assert "Suppressed" in output
        finally:
            NOTIFIER.muted_until = 0.0

    asyncio.run(run())


def test_activity_report_summarises_recorded_time(tools, memory):
    async def run():
        import time as _time

        now = _time.time()
        await memory.record_activity(
            app="Steam", title="", category="game", started=now - 3600, seconds=3600
        )
        output, is_error = await tools.execute("activity_report", {"hours": 24})
        assert not is_error
        assert "Steam" in output and "game" in output

    asyncio.run(run())


# -- helpers ---------------------------------------------------------------


def test_url_detection_covers_the_schemes_that_matter():
    assert _is_url("https://example.com")
    assert _is_url("spotify:track:abc")
    assert not _is_url("/home/user/song.mp3")
    assert not _is_url("C:\\Music\\song.mp3")
