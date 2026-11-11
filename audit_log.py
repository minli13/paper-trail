"""Append-only JSONL audit events that avoid recording user content or secrets."""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class AuditLogger:
    """Write one structured, content-minimized event per audit action."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._context: ContextVar[dict[str, str]] = ContextVar(
            f"audit_context_{id(self)}",
            default={},
        )

    def set_context(self, request_id: str, session_id: str | None = None) -> Token:
        """Bind request and privacy-preserving session identifiers to this context."""
        values = {"request_id": request_id}
        if session_id:
            values["session_id_hash"] = hashlib.sha256(
                session_id.encode("utf-8")
            ).hexdigest()
        return self._context.set(values)

    def reset_context(self, token: Token) -> None:
        self._context.reset(token)

    def log(
        self,
        action: str,
        outcome: str,
        metadata: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> None:
        """Append a UTC event; callers must pass metadata, never request content."""
        event: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "outcome": outcome,
        }
        event.update(self._context.get())
        if metadata:
            event["metadata"] = dict(metadata)

        encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as log_file:
                log_file.write(encoded)
            sys.stdout.write(encoded)
            sys.stdout.flush()
