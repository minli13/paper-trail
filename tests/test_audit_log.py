import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from audit_log import AuditLogger


class AuditLoggerTests(unittest.TestCase):
    def test_log_writes_same_json_event_to_file_and_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            logger = AuditLogger(path)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                logger.log("test.action", "success")

            file_event = path.read_text(encoding="utf-8")
            self.assertEqual(stdout.getvalue(), file_event)
            self.assertEqual(json.loads(file_event)["action"], "test.action")

    def test_context_adds_request_id_and_only_a_hashed_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            logger = AuditLogger(path)
            session_id = "12345678-1234-5678-1234-567812345678"
            token = logger.set_context("request-123", session_id)
            try:
                logger.log("test.action", "success")
            finally:
                logger.reset_context(token)

            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["request_id"], "request-123")
            self.assertNotIn(session_id, path.read_text(encoding="utf-8"))
            self.assertEqual(len(event["session_id_hash"]), 64)


if __name__ == "__main__":
    unittest.main()
