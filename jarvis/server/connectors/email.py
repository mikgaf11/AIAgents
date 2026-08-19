"""IMAP + SMTP email connector.

Deliberately built on stdlib `imaplib` / `smtplib` rather than a provider
API: one app password works with Gmail, Outlook, iCloud, Fastmail, Yahoo
and any self-hosted server, with no OAuth app registration and no extra
dependencies.

All blocking socket work is dispatched to worker threads so the reasoning
loop never stalls on the network.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage as OutboundMessage
from typing import Any

# Common providers, so the user only has to supply an address and password.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {
        "match": ("gmail.com", "googlemail.com"),
        "imap_host": "imap.gmail.com", "imap_port": 993,
        "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_ssl": False,
        "note": "Gmail needs a 16-character App Password (Google Account → "
                "Security → 2-Step Verification → App passwords), not your "
                "normal password.",
    },
    "outlook": {
        "match": ("outlook.com", "hotmail.com", "live.com", "msn.com"),
        "imap_host": "outlook.office365.com", "imap_port": 993,
        "smtp_host": "smtp-mail.outlook.com", "smtp_port": 587, "smtp_ssl": False,
        "note": "Outlook needs an app password if you have 2FA enabled.",
    },
    "icloud": {
        "match": ("icloud.com", "me.com", "mac.com"),
        "imap_host": "imap.mail.me.com", "imap_port": 993,
        "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_ssl": False,
        "note": "iCloud requires an app-specific password from appleid.apple.com.",
    },
    "fastmail": {
        "match": ("fastmail.com", "fastmail.fm"),
        "imap_host": "imap.fastmail.com", "imap_port": 993,
        "smtp_host": "smtp.fastmail.com", "smtp_port": 465, "smtp_ssl": True,
        "note": "Fastmail needs an app password from Settings → Privacy & Security.",
    },
    "yahoo": {
        "match": ("yahoo.com", "ymail.com"),
        "imap_host": "imap.mail.yahoo.com", "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_ssl": True,
        "note": "Yahoo requires an app password from Account Security.",
    },
    "proton": {
        "match": ("proton.me", "protonmail.com", "pm.me"),
        "imap_host": "127.0.0.1", "imap_port": 1143,
        "smtp_host": "127.0.0.1", "smtp_port": 1025, "smtp_ssl": False,
        "note": "Proton only speaks IMAP through Proton Mail Bridge, which must "
                "be installed and running locally.",
    },
}


def guess_provider(address: str) -> tuple[str | None, dict[str, Any]]:
    """Map an email address to provider settings, if we know the domain."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    for name, settings in PROVIDERS.items():
        if domain in settings["match"]:
            return name, settings
    return None, {}


def _decode(value: str | None) -> str:
    """Decode RFC 2047 encoded headers into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


@dataclass
class EmailMessage:
    uid: int
    message_id: str = ""
    sender: str = ""
    sender_email: str = ""
    to: str = ""
    subject: str = ""
    body: str = ""
    received: float = 0.0
    folder: str = "INBOX"
    flags: list[str] = field(default_factory=list)

    @property
    def snippet(self) -> str:
        collapsed = re.sub(r"\s+", " ", self.body).strip()
        return collapsed[:1200]

    def for_triage(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.received or time.time()))
        return (
            f"uid: {self.uid}\n"
            f"from: {self.sender} <{self.sender_email}>\n"
            f"received: {when}\n"
            f"subject: {self.subject}\n"
            f"body: {self.snippet[:900]}"
        )


class EmailError(RuntimeError):
    """Raised for anything the user can fix: bad credentials, wrong host."""


class EmailConnector:
    """A single mailbox, reachable over IMAP and SMTP."""

    def __init__(
        self,
        address: str,
        password: str,
        *,
        imap_host: str = "",
        imap_port: int = 993,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_ssl: bool = False,
        username: str = "",
        folder: str = "INBOX",
    ) -> None:
        provider, settings = guess_provider(address)
        self.provider = provider
        self.address = address
        self.username = username or address
        self.password = password
        self.imap_host = imap_host or settings.get("imap_host", "")
        self.imap_port = imap_port or settings.get("imap_port", 993)
        self.smtp_host = smtp_host or settings.get("smtp_host", "")
        self.smtp_port = smtp_port or settings.get("smtp_port", 587)
        self.smtp_ssl = smtp_ssl if smtp_host else settings.get("smtp_ssl", False)
        self.folder = folder

    @property
    def configured(self) -> bool:
        return bool(self.address and self.password and self.imap_host)

    # -- IMAP plumbing ---------------------------------------------------

    def _connect(self) -> imaplib.IMAP4:
        try:
            if self.imap_port == 143:
                client = imaplib.IMAP4(self.imap_host, self.imap_port)
                client.starttls(ssl.create_default_context())
            else:
                client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            client.login(self.username, self.password)
            return client
        except imaplib.IMAP4.error as exc:
            raise EmailError(
                f"IMAP login failed for {self.address}: {exc}. "
                "Most providers need an app password rather than your normal one."
            ) from exc
        except OSError as exc:
            raise EmailError(
                f"Could not reach {self.imap_host}:{self.imap_port} — {exc}"
            ) from exc

    def _fetch_sync(self, since_uid: int, limit: int) -> list[EmailMessage]:
        client = self._connect()
        try:
            status, _ = client.select(self.folder, readonly=False)
            if status != "OK":
                raise EmailError(f"Could not open folder {self.folder}")

            # UID SEARCH is stable across sessions; sequence numbers are not.
            criterion = f"{max(1, since_uid + 1)}:*"
            status, data = client.uid("SEARCH", None, "UID", criterion)
            if status != "OK" or not data or not data[0]:
                return []

            uids = [int(u) for u in data[0].split()]
            # The `n:*` form always returns at least one message even when
            # nothing is newer, so drop anything we've already handled.
            uids = [u for u in uids if u > since_uid][-limit:]
            if not uids:
                return []

            messages: list[EmailMessage] = []
            for uid in uids:
                status, payload = client.uid("FETCH", str(uid), "(BODY.PEEK[] FLAGS)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                raw = payload[0][1]
                flags_blob = payload[0][0].decode("utf-8", "replace")
                messages.append(self._parse(uid, raw, flags_blob))
            return messages
        finally:
            try:
                client.close()
            except (imaplib.IMAP4.error, OSError):
                pass
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def _parse(self, uid: int, raw: bytes, flags_blob: str = "") -> EmailMessage:
        parsed = email.message_from_bytes(raw)
        sender_name, sender_email = email.utils.parseaddr(parsed.get("From", ""))

        received = 0.0
        date_header = parsed.get("Date")
        if date_header:
            try:
                received = email.utils.parsedate_to_datetime(date_header).timestamp()
            except (TypeError, ValueError):
                received = 0.0

        return EmailMessage(
            uid=uid,
            message_id=(parsed.get("Message-ID") or "").strip(),
            sender=_decode(sender_name) or sender_email,
            sender_email=sender_email,
            to=_decode(parsed.get("To")),
            subject=_decode(parsed.get("Subject")) or "(no subject)",
            body=self._extract_body(parsed),
            received=received or time.time(),
            folder=self.folder,
            flags=re.findall(r"\\(\w+)", flags_blob),
        )

    @staticmethod
    def _extract_body(parsed) -> str:
        """Prefer text/plain; fall back to HTML with the tags stripped."""
        plain, html = "", ""
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_filename():
                    continue  # attachment
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text = part.get_payload(decode=True).decode(charset, "replace")
                except (LookupError, UnicodeDecodeError, AttributeError, TypeError):
                    continue
                if content_type == "text/plain" and not plain:
                    plain = text
                elif content_type == "text/html" and not html:
                    html = text
        else:
            try:
                charset = parsed.get_content_charset() or "utf-8"
                payload = parsed.get_payload(decode=True)
                text = payload.decode(charset, "replace") if payload else ""
            except (LookupError, UnicodeDecodeError, AttributeError, TypeError):
                text = ""
            if parsed.get_content_type() == "text/html":
                html = text
            else:
                plain = text

        if plain.strip():
            return plain
        if html.strip():
            stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html,
                              flags=re.S | re.I)
            stripped = re.sub(r"<[^>]+>", " ", stripped)
            return re.sub(r"&(nbsp|amp|lt|gt|quot|#39);", " ", stripped)
        return ""

    # -- async surface ---------------------------------------------------

    async def fetch_new(self, since_uid: int, limit: int = 25) -> list[EmailMessage]:
        return await asyncio.to_thread(self._fetch_sync, since_uid, limit)

    async def probe(self) -> dict[str, Any]:
        """Verify credentials. Used by the setup wizard and the status panel."""

        def _check() -> dict[str, Any]:
            client = self._connect()
            try:
                status, data = client.select(self.folder, readonly=True)
                count = int(data[0]) if status == "OK" and data and data[0] else 0
                return {"ok": True, "folder": self.folder, "messages": count}
            finally:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

        try:
            return await asyncio.to_thread(_check)
        except EmailError as exc:
            return {"ok": False, "error": str(exc)}

    async def set_flags(self, uid: int, *flags: str, remove: bool = False) -> bool:
        """Flag a message in the real mailbox, so triage shows in any client."""

        def _apply() -> bool:
            client = self._connect()
            try:
                client.select(self.folder, readonly=False)
                command = "-FLAGS" if remove else "+FLAGS"
                status, _ = client.uid("STORE", str(uid), command,
                                       "(" + " ".join(flags) + ")")
                return status == "OK"
            finally:
                try:
                    client.close()
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

        try:
            return await asyncio.to_thread(_apply)
        except EmailError:
            return False

    async def send(
        self, to_addr: str, subject: str, body: str, *, in_reply_to: str = ""
    ) -> None:
        """Send a message. Only ever called after explicit user approval."""

        def _send() -> None:
            message = OutboundMessage()
            message["From"] = self.address
            message["To"] = to_addr
            message["Subject"] = subject
            message["Date"] = email.utils.formatdate(localtime=True)
            message["Message-ID"] = email.utils.make_msgid()
            if in_reply_to:
                message["In-Reply-To"] = in_reply_to
                message["References"] = in_reply_to
            message.set_content(body)

            context = ssl.create_default_context()
            try:
                if self.smtp_ssl:
                    server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port,
                                              context=context, timeout=30)
                else:
                    server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30)
                    server.starttls(context=context)
                with server:
                    server.login(self.username, self.password)
                    server.send_message(message)
            except smtplib.SMTPAuthenticationError as exc:
                raise EmailError(f"SMTP login rejected: {exc}") from exc
            except (smtplib.SMTPException, OSError) as exc:
                raise EmailError(f"Could not send mail: {exc}") from exc

        await asyncio.to_thread(_send)


def from_config(config) -> EmailConnector | None:
    """Build a connector from configuration, or None if email isn't set up."""
    if not (config.email_address and config.email_password):
        return None
    return EmailConnector(
        config.email_address,
        config.email_password,
        imap_host=config.email_imap_host,
        imap_port=config.email_imap_port,
        smtp_host=config.email_smtp_host,
        smtp_port=config.email_smtp_port,
        smtp_ssl=config.email_smtp_ssl,
        folder=config.email_folder,
    )
