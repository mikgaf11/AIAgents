"""External services JARVIS can reach.

Each connector is self-contained and optional: if it isn't configured, the
rest of the system carries on without it. Email is the one wired up today;
this package is the seam where calendar, messaging or anything else slots
in the same way.
"""

from .email import EmailConnector, EmailMessage, PROVIDERS, guess_provider

__all__ = ["EmailConnector", "EmailMessage", "PROVIDERS", "guess_provider"]
