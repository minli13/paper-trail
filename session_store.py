"""Ephemeral per-browser sessions backed by instance-local directories."""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


DEFAULT_SAMPLE_DOCUMENTS = (
    "employee-handbook.md",
    "security-faq.md",
    "travel-policy.txt",
)


@dataclass(frozen=True)
class UserSession:
    session_id: str
    root: Path
    documents_dir: Path
    notes_dir: Path
    is_new: bool


class SessionStore:
    """Create, seed, touch, and expire sessions under a bounded root."""

    def __init__(
        self,
        data_dir: str | Path,
        sample_documents_dir: str | Path,
        idle_timeout_seconds: float = 3 * 60 * 60,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        self.root = Path(data_dir).resolve() / "sessions"
        self.sample_documents_dir = Path(sample_documents_dir).resolve()
        self.idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.RLock()

    def open_session(self, cookie_value: str | None = None) -> UserSession:
        """Resolve a cookie UUID or create a fresh, sample-seeded session."""
        with self._lock:
            self.cleanup_idle()
            session_id = self._normalize_id(cookie_value)
            session_root = self.root / session_id
            is_new = not session_root.exists()

            if session_root.is_symlink():
                session_id = str(uuid.uuid4())
                session_root = self.root / session_id
                is_new = True

            documents_dir = session_root / "documents"
            notes_dir = session_root / "notes"
            documents_dir.mkdir(parents=True, exist_ok=True)
            notes_dir.mkdir(parents=True, exist_ok=True)
            if is_new:
                self._seed_documents(documents_dir)
            self._touch(session_root)
            return UserSession(
                session_id=session_id,
                root=session_root,
                documents_dir=documents_dir,
                notes_dir=notes_dir,
                is_new=is_new,
            )

    def touch(self, session: UserSession) -> None:
        with self._lock:
            self._touch(session.root)

    def cleanup_idle(self, now: float | None = None) -> list[str]:
        """Delete expired UUID session directories and return deleted IDs."""
        current_time = time.time() if now is None else now
        deleted: list[str] = []
        with self._lock:
            if not self.root.is_dir():
                return deleted
            for entry in self.root.iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    continue
                try:
                    session_id = str(uuid.UUID(entry.name))
                except ValueError:
                    continue
                activity = entry / ".last_activity"
                try:
                    last_activity = activity.stat().st_mtime
                except FileNotFoundError:
                    last_activity = entry.stat().st_mtime
                if current_time - last_activity < self.idle_timeout_seconds:
                    continue
                shutil.rmtree(entry)
                deleted.append(session_id)
        return deleted

    @staticmethod
    def _normalize_id(cookie_value: str | None) -> str:
        if cookie_value:
            try:
                parsed = uuid.UUID(cookie_value)
                return str(parsed)
            except (ValueError, AttributeError):
                pass
        return str(uuid.uuid4())

    def _seed_documents(self, destination: Path) -> None:
        for filename in DEFAULT_SAMPLE_DOCUMENTS:
            source = self.sample_documents_dir / filename
            if not source.is_file():
                raise FileNotFoundError(f"Required sample document is missing: {source}")
            (destination / filename).write_bytes(source.read_bytes())

    @staticmethod
    def _touch(session_root: Path) -> None:
        marker = session_root / ".last_activity"
        marker.touch(exist_ok=True)
        os.utime(marker, None)
