import os
import tempfile
import time
import unittest
from pathlib import Path

from session_store import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.sample_documents_dir = self.root / "samples"
        self.sample_documents_dir.mkdir()
        for filename in (
            "employee-handbook.md",
            "security-faq.md",
            "travel-policy.txt",
        ):
            (self.sample_documents_dir / filename).write_text(
                f"Sample content for {filename}",
                encoding="utf-8",
            )
        self.store = SessionStore(
            self.root / "data",
            self.sample_documents_dir,
            idle_timeout_seconds=3600,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_session_is_seeded_and_cookie_id_is_reused(self) -> None:
        session = self.store.open_session()
        self.assertTrue(session.is_new)
        self.assertEqual(
            sorted(path.name for path in session.documents_dir.iterdir()),
            ["employee-handbook.md", "security-faq.md", "travel-policy.txt"],
        )
        reopened = self.store.open_session(session.session_id)
        self.assertFalse(reopened.is_new)
        self.assertEqual(reopened.root, session.root)

    def test_cleanup_removes_only_expired_uuid_session_directories(self) -> None:
        expired = self.store.open_session()
        active = self.store.open_session()
        unrelated = self.store.root / "not-a-session"
        unrelated.mkdir()
        expired_at = time.time() - 7200
        os.utime(expired.root / ".last_activity", (expired_at, expired_at))

        deleted = self.store.cleanup_idle()

        self.assertEqual(deleted, [expired.session_id])
        self.assertFalse(expired.root.exists())
        self.assertTrue(active.root.exists())
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
