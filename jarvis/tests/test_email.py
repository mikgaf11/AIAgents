"""Email connector parsing and autonomous triage.

No network: the connector's parser is exercised on raw RFC-822 bytes, and
the triage engine runs against a fake mailbox and a scripted classifier.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.connectors.email import (  # noqa: E402
    EmailConnector,
    EmailMessage,
    guess_provider,
)
from server.memory import Memory  # noqa: E402
from server.triage import TriageEngine, _clamp_priority, _parse_json_array  # noqa: E402


@pytest.fixture()
def connector():
    return EmailConnector("someone@gmail.com", "app-password")


@pytest.fixture()
def memory(tmp_path):
    store = Memory(tmp_path / "mail.db")
    yield store
    store.close()


# -- provider detection ----------------------------------------------------


def test_known_providers_are_auto_configured():
    name, settings = guess_provider("someone@gmail.com")
    assert name == "gmail"
    assert settings["imap_host"] == "imap.gmail.com"
    assert "App Password" in settings["note"]


def test_provider_detection_is_case_and_domain_aware():
    assert guess_provider("Someone@ICLOUD.com")[0] == "icloud"
    assert guess_provider("me@my-own-server.net")[0] is None


def test_connector_infers_settings_from_the_address(connector):
    assert connector.imap_host == "imap.gmail.com"
    assert connector.smtp_host == "smtp.gmail.com"
    assert connector.configured


def test_explicit_hosts_override_the_preset():
    custom = EmailConnector(
        "me@example.com", "pw", imap_host="mail.example.com", imap_port=143
    )
    assert custom.imap_host == "mail.example.com"
    assert custom.provider is None


# -- message parsing -------------------------------------------------------

PLAIN = b"""\
From: Ada Lovelace <ada@example.com>
To: me@example.com
Subject: Engine schedule
Date: Tue, 18 Aug 2026 09:14:00 +0000
Message-ID: <abc123@example.com>
Content-Type: text/plain; charset="utf-8"

The analytical engine ships Thursday.
Please confirm.
"""

MULTIPART = b"""\
From: "Bob" <bob@example.com>
Subject: =?utf-8?B?SW52b2ljZSDCozEyMA==?=
Date: Wed, 19 Aug 2026 11:00:00 +0000
Content-Type: multipart/alternative; boundary="XX"

--XX
Content-Type: text/plain; charset="utf-8"

Invoice attached, due Friday.
--XX
Content-Type: text/html; charset="utf-8"

<html><body><p>Invoice attached, due Friday.</p></body></html>
--XX--
"""

HTML_ONLY = b"""\
From: news@example.com
Subject: Weekly digest
Content-Type: text/html; charset="utf-8"

<html><head><style>p{color:red}</style></head>
<body><p>Top&nbsp;story</p><script>alert(1)</script></body></html>
"""


def test_parses_a_plain_message(connector):
    message = connector._parse(42, PLAIN)
    assert message.uid == 42
    assert message.sender == "Ada Lovelace"
    assert message.sender_email == "ada@example.com"
    assert message.subject == "Engine schedule"
    assert "analytical engine ships Thursday" in message.body
    assert message.received > 0
    assert message.message_id == "<abc123@example.com>"


def test_decodes_encoded_subjects_and_prefers_plain_text(connector):
    message = connector._parse(43, MULTIPART)
    assert message.subject == "Invoice £120"
    # text/plain must win over the HTML alternative.
    assert message.body.strip() == "Invoice attached, due Friday."
    assert "<html>" not in message.body


def test_falls_back_to_html_with_tags_and_scripts_stripped(connector):
    message = connector._parse(44, HTML_ONLY)
    assert "Top" in message.body
    assert "<p>" not in message.body
    # Script and style contents must not leak into what the model reads.
    assert "alert" not in message.body
    assert "color:red" not in message.body


def test_snippet_collapses_whitespace_and_bounds_length(connector):
    message = connector._parse(45, PLAIN)
    assert "\n" not in message.snippet
    assert len(message.snippet) <= 1200


def test_missing_headers_do_not_crash_the_parser(connector):
    message = connector._parse(46, b"Content-Type: text/plain\n\nnaked body\n")
    assert message.subject == "(no subject)"
    assert message.sender_email == ""
    assert "naked body" in message.body
    assert message.received > 0  # falls back to now


def test_triage_view_includes_the_fields_the_classifier_needs(connector):
    rendered = connector._parse(47, PLAIN).for_triage()
    for expected in ("uid: 47", "from: Ada Lovelace", "subject: Engine schedule"):
        assert expected in rendered


# -- triage ----------------------------------------------------------------


def test_json_array_survives_fences_and_surrounding_prose():
    assert _parse_json_array('Here you go:\n```json\n[{"uid": 1}]\n```') == [{"uid": 1}]
    assert _parse_json_array("[]") == []
    assert _parse_json_array("not json at all") == []
    assert _parse_json_array("") == []


def test_priority_is_clamped_to_the_scale():
    assert _clamp_priority(9) == 4
    assert _clamp_priority(-3) == 0
    assert _clamp_priority("oops") == 2
    assert _clamp_priority(None) == 2


class FakeConnector:
    """A mailbox that yields a fixed set of messages and records flagging."""

    def __init__(self, messages):
        self.address = "someone@example.com"
        self.provider = "test"
        self.configured = True
        self.messages = messages
        self.flagged: list[int] = []
        self.fetched_since: list[int] = []

    async def fetch_new(self, since_uid, limit=25):
        self.fetched_since.append(since_uid)
        return [m for m in self.messages if m.uid > since_uid][:limit]

    async def set_flags(self, uid, *flags, remove=False):
        self.flagged.append(uid)
        return True


class FakeClassifier:
    """Returns a scripted verdict per uid."""

    def __init__(self, verdicts):
        self.messages = self
        self.verdicts = verdicts
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        import json

        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=json.dumps(self.verdicts))],
        )


def make_message(uid, subject="Hello", sender="Ada"):
    return EmailMessage(
        uid=uid, sender=sender, sender_email=f"{sender.lower()}@example.com",
        subject=subject, body="body text", received=1_760_000_000.0,
    )


def test_sweep_triages_stores_and_flags_important_mail(memory):
    inbox = FakeConnector([make_message(10, "Server on fire"), make_message(11, "Sale")])
    classifier = FakeClassifier([
        {"uid": 10, "priority": 4, "category": "work", "summary": "production down",
         "action": "call ops", "needs_reply": True},
        {"uid": 11, "priority": 0, "category": "newsletter", "summary": "marketing",
         "action": "", "needs_reply": False},
    ])
    engine = TriageEngine(memory, inbox, classifier)

    records = asyncio.run(engine.sweep())

    assert len(records) == 2
    assert records[0]["priority_label"] == "critical"
    # Only the important one gets flagged in the real mailbox.
    assert inbox.flagged == [10]
    stored = asyncio.run(memory.list_emails())
    assert {s["subject"] for s in stored} == {"Server on fire", "Sale"}
    urgent = asyncio.run(memory.urgent_emails())
    assert [u["subject"] for u in urgent] == ["Server on fire"]


def test_sweep_advances_the_uid_cursor_and_never_reprocesses(memory):
    inbox = FakeConnector([make_message(10), make_message(11)])
    classifier = FakeClassifier([
        {"uid": 10, "priority": 2, "summary": "a"},
        {"uid": 11, "priority": 2, "summary": "b"},
    ])
    engine = TriageEngine(memory, inbox, classifier)

    first = asyncio.run(engine.sweep())
    second = asyncio.run(engine.sweep())

    assert len(first) == 2
    assert second == [], "already-seen mail must not be triaged twice"
    assert inbox.fetched_since == [0, 11], "cursor must advance past the last uid"
    assert classifier.calls == 1, "a second model call is wasted money"


def test_duplicate_uids_are_rejected_by_storage(memory):
    message = {"account": "a", "folder": "INBOX", "uid": 5, "subject": "One"}
    first = asyncio.run(memory.record_email(message))
    second = asyncio.run(memory.record_email(message))
    assert isinstance(first, int)
    assert second is None


def test_classifier_failure_leaves_mail_untriaged_rather_than_mislabelled(memory):
    class Exploding:
        def __init__(self):
            self.messages = self

        async def create(self, **_):
            raise RuntimeError("model unavailable")

    inbox = FakeConnector([make_message(10, "Important")])
    engine = TriageEngine(memory, inbox, Exploding())

    records = asyncio.run(engine.sweep())

    # It still records the mail (so it isn't lost) at the neutral default,
    # rather than guessing a priority it has no basis for.
    assert len(records) == 1
    assert records[0]["priority"] == 2
    assert records[0]["summary"] == ""


def test_engine_is_disabled_without_configuration(memory):
    assert TriageEngine(memory, None, object()).enabled is False
    assert TriageEngine(memory, FakeConnector([]), None).enabled is False


def test_verdicts_fall_back_to_position_when_uids_are_wrong(memory):
    inbox = FakeConnector([make_message(10, "First"), make_message(11, "Second")])
    # The model returned uids that don't exist; order must still be honoured.
    classifier = FakeClassifier([
        {"uid": 999, "priority": 4, "summary": "first"},
        {"uid": 998, "priority": 1, "summary": "second"},
    ])
    engine = TriageEngine(memory, inbox, classifier)
    records = asyncio.run(engine.sweep())
    by_subject = {r["subject"]: r for r in records}
    assert by_subject["First"]["priority"] == 4
    assert by_subject["Second"]["priority"] == 1
