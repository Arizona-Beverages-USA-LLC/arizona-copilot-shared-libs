"""
In-memory session registry for in-progress documents.

Each document-in-progress (XLSX workbook being built, PDF being assembled,
etc.) lives as an entry in a module-level dict keyed by session_id. The
agent works through one document across multiple tool calls, passing the
session_id each time, until it calls the format's finalize_* tool.

Sessions automatically expire after SESSION_TTL_SECONDS to prevent leaks
if the agent abandons a build midway (crash, timeout, user pivot).

Thread-safety note: Agent Engine runs each request in a single async task,
so there's no concurrent access from within one request. Multiple concurrent
requests would each have distinct session_ids so no collision.
"""

from __future__ import annotations

import secrets
import time
from typing import Dict, Optional

from ._types import BaseSession


SESSION_TTL_SECONDS = 30 * 60  # 30 minutes — generous; real builds finish in seconds


# Module-level registry. Keyed by session_id (short random string).
_SESSIONS: Dict[str, BaseSession] = {}


def new_session_id() -> str:
    """Generate a short, URL-safe random id. 8 chars = 48 bits of entropy,
    plenty for a process lifetime with a handful of concurrent builds."""
    return secrets.token_urlsafe(6)  # ~8 chars


def register(session: BaseSession) -> None:
    """Add a session to the registry. Also sweeps expired sessions opportunistically."""
    _sweep_expired()
    _SESSIONS[session.session_id] = session


def get(session_id: str, expected_format: Optional[str] = None) -> BaseSession:
    """Retrieve a session by id. Raises ValueError if missing, expired, or
    the format doesn't match what the caller expected.

    expected_format lets each tool guard against cross-format misuse — e.g.
    an XLSX add_rows call against a PDF session id.
    """
    _sweep_expired()
    session = _SESSIONS.get(session_id)
    if session is None:
        raise ValueError(
            f"Export session not found: {session_id!r}. "
            f"Either the session expired (>{SESSION_TTL_SECONDS // 60} min old) "
            f"or it was never created. Start a new document with the appropriate "
            f"new_* tool."
        )
    if expected_format is not None and session.format != expected_format:
        raise ValueError(
            f"Session {session_id!r} is a {session.format!r} session, "
            f"not {expected_format!r}. Use the correct format's tool."
        )
    return session


def drop(session_id: str) -> None:
    """Remove a session (typically after finalize). Silent if already gone."""
    _SESSIONS.pop(session_id, None)


def _sweep_expired() -> None:
    """Remove sessions older than SESSION_TTL_SECONDS. Called on every
    register/get; cheap at the scale of tens of concurrent sessions."""
    cutoff = time.time() - SESSION_TTL_SECONDS
    expired = [sid for sid, s in _SESSIONS.items() if s.created_at < cutoff]
    for sid in expired:
        _SESSIONS.pop(sid, None)


# ----------------------------------------------------------------------------
# Testing / diagnostic helpers
# ----------------------------------------------------------------------------

def _count() -> int:
    """Return current session count. Used by tests."""
    return len(_SESSIONS)


def _reset() -> None:
    """Clear all sessions. Used by tests. Never call from production code."""
    _SESSIONS.clear()
