import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from audit_log import AuditLogger
from faq_agent import FAQAgent
from web_app import create_app


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.sample_documents_dir = self.root / "sample-documents"
        self.sample_documents_dir.mkdir()
        (self.sample_documents_dir / "employee-handbook.md").write_text(
            "Keep records for seven years.",
            encoding="utf-8",
        )
        (self.sample_documents_dir / "security-faq.md").write_text(
            "Use unique passwords.",
            encoding="utf-8",
        )
        (self.sample_documents_dir / "travel-policy.txt").write_text(
            "Submit travel expenses promptly.",
            encoding="utf-8",
        )
        self.data_dir = self.root / "data"

        def fake_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            return "Keep records for seven years. [1]"

        agent = FAQAgent(
            self.sample_documents_dir,
            self.root / "prototype-notes",
            llm_call=fake_llm,
        )
        self.audit_path = self.root / "audit.jsonl"
        self.client = TestClient(
            create_app(
                agent,
                audit_logger=AuditLogger(self.audit_path),
                sample_documents_dir=self.sample_documents_dir,
                data_dir=self.data_dir,
            )
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def session_dir(self, client: TestClient | None = None) -> Path:
        active_client = client or self.client
        session_id = active_client.cookies.get("paper_trail_session")
        self.assertIsNotNone(session_id)
        self.assertEqual(str(uuid.UUID(session_id)), session_id)
        return self.data_dir / "sessions" / session_id

    def test_homepage_and_static_frontend_are_served(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Paper Trail", page.text)
        self.assertIn(
            "Your documents are temporary and may be cleared when the server restarts.",
            page.text,
        )
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/ask", script.text)
        self.assertIn("Actions taken", script.text)

    def test_documents_and_ask_api(self) -> None:
        documents = self.client.get("/api/documents")
        response = self.client.post(
            "/api/ask",
            json={"question": "How long should records be kept?"},
        )

        self.assertEqual(
            documents.json(),
            {
                "documents": [
                    "employee-handbook.md",
                    "security-faq.md",
                    "travel-policy.txt",
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Keep records for seven years. [1]")
        self.assertEqual(response.json()["sources"], ["employee-handbook.md"])
        self.assertEqual(
            [item["command"] for item in response.json()["operations"]],
            ["SEARCH", "ANSWER"],
        )
        self.assertEqual(
            [item["status"] for item in response.json()["operations"]],
            ["completed", "completed"],
        )
        self.assertTrue(
            all(item["duration_ms"] >= 0 for item in response.json()["operations"])
        )
        self.assertTrue(
            all(
                item["feedback"]
                for item in response.json()["operations"]
            )
        )
        self.assertEqual(
            set(response.json()["timings"]),
            {"planning_ms", "execution_ms", "total_ms"},
        )
        self.assertTrue(
            all("arguments" not in item for item in response.json()["operations"])
        )
        self.assertEqual(
            response.json()["evidence"][0]["text"],
            "Keep records for seven years.",
        )
        audit_contents = self.audit_path.read_text(encoding="utf-8")
        events = [json.loads(line) for line in audit_contents.splitlines()]
        actions = {event["action"] for event in events}
        self.assertIn("faq.ask", actions)
        self.assertIn("dsl.command", actions)
        self.assertIn("http.request", actions)
        self.assertNotIn("How long should records be kept?", audit_contents)
        self.assertNotIn("Keep records for seven years. [1]", audit_contents)

    def test_ask_api_returns_answer_text_not_llm_json_object(self) -> None:
        def json_answer_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            return '{"answer": "Keep records for seven years. [1]"}'

        agent = FAQAgent(
            self.sample_documents_dir,
            self.root / "json-answer-notes",
            llm_call=json_answer_llm,
        )
        client = TestClient(
            create_app(
                agent,
                audit_logger=AuditLogger(self.root / "json-answer-audit.jsonl"),
                sample_documents_dir=self.sample_documents_dir,
                data_dir=self.root / "json-answer-data",
            )
        )
        try:
            response = client.post("/api/ask", json={"question": "How long?"})
        finally:
            client.close()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Keep records for seven years. [1]")
        self.assertIsInstance(response.json()["answer"], str)

    def test_api_returns_partial_action_trace_when_operation_fails(self) -> None:
        def failing_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            raise RuntimeError("model unavailable")

        failing_agent = FAQAgent(
            self.sample_documents_dir,
            self.root / "prototype-notes",
            llm_call=failing_llm,
        )
        client = TestClient(
            create_app(
                failing_agent,
                audit_logger=AuditLogger(self.root / "failed-audit.jsonl"),
                sample_documents_dir=self.sample_documents_dir,
                data_dir=self.root / "failed-data",
            )
        )
        try:
            response = client.post("/api/ask", json={"question": "How long?"})
        finally:
            client.close()

        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(
            [item["status"] for item in body["operations"]],
            ["completed", "failed"],
        )
        self.assertIn("RuntimeError", body["operations"][1]["feedback"])
        self.assertGreaterEqual(body["timings"]["total_ms"], 0)

    def test_note_preview_does_not_save_until_confirmed(self) -> None:
        note = {
            "title": "Retention",
            "content": "Keep records for seven years. [1]",
            "sources": ["employee-handbook.md"],
        }

        preview = self.client.post("/api/notes", json=note)
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.json()["saved"])
        notes_dir = self.session_dir() / "notes"
        self.assertFalse(list(notes_dir.glob("*.md")))

        saved = self.client.post(
            "/api/notes",
            json={**note, "confirmed": True},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["saved"])
        self.assertIn("Keep records for seven years.", saved.json()["preview"])
        self.assertTrue((notes_dir / saved.json()["filename"]).is_file())
        events = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        note_events = [event for event in events if event["action"] == "note.save"]
        self.assertEqual(
            [event["outcome"] for event in note_events],
            ["not_confirmed", "saved"],
        )

    def test_note_api_rejects_unknown_sources(self) -> None:
        response = self.client.post(
            "/api/notes",
            json={
                "title": "Questionable",
                "content": "Text",
                "sources": ["not-a-document.md"],
                "confirmed": True,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Unknown source", response.json()["detail"])

    def test_document_upload_adds_utf8_text_to_library(self) -> None:
        response = self.client.post(
            "/api/documents",
            files={"file": ("new-info.md", b"Useful local information.", "text/markdown")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["filename"], "new-info.md")
        self.assertEqual(
            self.client.get("/api/documents").json()["documents"],
            [
                "employee-handbook.md",
                "new-info.md",
                "security-faq.md",
                "travel-policy.txt",
            ],
        )
        self.assertEqual(
            (self.session_dir() / "documents" / "new-info.md").read_text(encoding="utf-8"),
            "Useful local information.",
        )

    def test_document_upload_rejects_unsupported_and_oversized_files(self) -> None:
        unsupported = self.client.post(
            "/api/documents",
            files={"file": ("image.png", b"not a text document", "image/png")},
        )
        oversized = self.client.post(
            "/api/documents",
            files={"file": ("large.md", b"x" * (5 * 1024 * 1024 + 1), "text/markdown")},
        )

        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual(oversized.status_code, 413)

    def test_document_delete_removes_document_and_returns_no_json_body(self) -> None:
        response = self.client.delete("/api/documents/employee-handbook.md")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertNotIn(
            "employee-handbook.md",
            self.client.get("/api/documents").json()["documents"],
        )
        events = [
            json.loads(line)
            for line in self.audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn(
            ("document.delete", "deleted"),
            [(event["action"], event["outcome"]) for event in events],
        )

    def test_document_delete_rejects_unknown_and_traversal_paths(self) -> None:
        unknown = self.client.delete("/api/documents/missing.md")
        traversal = self.client.delete("/api/documents/..%5Csecurity-faq.md")

        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(traversal.status_code, 404)
        self.assertIn(
            "security-faq.md",
            self.client.get("/api/documents").json()["documents"],
        )

    def test_upload_strips_client_path_and_avoids_overwrite(self) -> None:
        first = self.client.post(
            "/api/documents",
            files={"file": ("..\\new-upload.md", b"Uploaded version.", "text/markdown")},
        )
        second = self.client.post(
            "/api/documents",
            files={"file": ("new-upload.md", b"Another upload.", "text/markdown")},
        )

        self.assertEqual(first.json()["filename"], "new-upload.md")
        self.assertEqual(second.json()["filename"], "new-upload-2.md")
        self.assertEqual(
            (
                self.session_dir()
                / "documents"
                / "employee-handbook.md"
            ).read_text(encoding="utf-8"),
            "Keep records for seven years.",
        )

    def test_saved_note_has_download_link_and_download_endpoint(self) -> None:
        note = {
            "title": "Retention",
            "content": "Keep records for seven years. [1]",
            "sources": ["employee-handbook.md"],
            "confirmed": True,
        }
        saved = self.client.post("/api/notes", json=note).json()

        self.assertEqual(saved["filename"], "Retention.md")
        download = self.client.get(saved["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertIn("Keep records for seven years.", download.text)
        self.assertIn("attachment", download.headers["content-disposition"])

    def test_note_download_rejects_path_traversal(self) -> None:
        response = self.client.get(
            "/api/notes/download",
            params={"filename": "..\\documents\\policy.md"},
        )

        self.assertEqual(response.status_code, 404)

    def test_sessions_seed_documents_and_isolate_uploads_and_notes(self) -> None:
        other_client = TestClient(self.client.app)
        try:
            first_docs = self.client.get("/api/documents").json()["documents"]
            second_docs = other_client.get("/api/documents").json()["documents"]
            first_session = self.session_dir(self.client)
            second_session = self.session_dir(other_client)

            self.assertNotEqual(first_session, second_session)
            self.assertEqual(first_docs, second_docs)
            self.assertEqual(
                first_docs,
                ["employee-handbook.md", "security-faq.md", "travel-policy.txt"],
            )

            uploaded = self.client.post(
                "/api/documents",
                files={"file": ("private.md", b"first session only", "text/markdown")},
            )
            note = self.client.post(
                "/api/notes",
                json={
                    "title": "Private note",
                    "content": "Saved in one session.",
                    "sources": ["employee-handbook.md"],
                    "confirmed": True,
                },
            ).json()

            self.assertEqual(uploaded.status_code, 200)
            self.assertIn(
                "private.md",
                self.client.get("/api/documents").json()["documents"],
            )
            self.assertNotIn(
                "private.md",
                other_client.get("/api/documents").json()["documents"],
            )
            self.assertTrue((first_session / "notes" / note["filename"]).is_file())
            isolated_download = other_client.get(note["download_url"])
            self.assertEqual(isolated_download.status_code, 404)
        finally:
            other_client.close()

    def test_invalid_session_cookie_is_replaced_with_a_seeded_session(self) -> None:
        response = self.client.get(
            "/api/documents",
            headers={"Cookie": "paper_trail_session=not-a-uuid"},
        )

        self.assertEqual(response.status_code, 200)
        session_id = response.cookies.get("paper_trail_session")
        self.assertEqual(len(session_id), 36)
        self.assertEqual(
            len(list((self.data_dir / "sessions").iterdir())),
            1,
        )
        self.assertEqual(
            response.json()["documents"],
            ["employee-handbook.md", "security-faq.md", "travel-policy.txt"],
        )

    def test_upload_enforces_per_session_document_and_storage_caps(self) -> None:
        with patch("web_app.MAX_SESSION_DOCUMENTS", 3):
            count_limited = self.client.post(
                "/api/documents",
                files={"file": ("fourth.md", b"content", "text/markdown")},
            )
        self.assertEqual(count_limited.status_code, 413)

        with patch("web_app.MAX_SESSION_STORAGE_BYTES", 1):
            size_limited = self.client.post(
                "/api/documents",
                files={"file": ("too-large-for-session.md", b"content", "text/markdown")},
            )
        self.assertEqual(size_limited.status_code, 413)


if __name__ == "__main__":
    unittest.main()
