"""Append-only JSONL audit events that avoid recording user content or secrets."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


class AuditLogger:
    """Write one structured, content-minimized event per audit action."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

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
        if metadata:
            event["metadata"] = dict(metadata)

        encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as log_file:
                log_file.write(encoded)
