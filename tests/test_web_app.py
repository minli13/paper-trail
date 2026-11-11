import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from faq_agent import FAQAgent
from web_app import create_app


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.documents_dir = self.root / "documents"
        self.documents_dir.mkdir()
        (self.documents_dir / "policy.md").write_text(
            "Keep records for seven years.",
            encoding="utf-8",
        )
        self.notes_dir = self.root / "notes"

        def fake_llm(prompt: str) -> str:
            if "action planner" in prompt:
                return (
                    'SEARCH("seven years")\n'
                    'ANSWER("How long?", "$SEARCH_RESULTS")'
                )
            return "Keep records for seven years. [1]"

        agent = FAQAgent(
            self.documents_dir,
            self.notes_dir,
            llm_call=fake_llm,
        )
        self.client = TestClient(create_app(agent))

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_homepage_and_static_frontend_are_served(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Paper Trail", page.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/ask", script.text)

    def test_documents_and_ask_api(self) -> None:
        documents = self.client.get("/api/documents")
        response = self.client.post(
            "/api/ask",
            json={"question": "How long should records be kept?"},
        )

        self.assertEqual(documents.json(), {"documents": ["policy.md"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Keep records for seven years. [1]")
        self.assertEqual(response.json()["sources"], ["policy.md"])
        self.assertEqual(
            response.json()["evidence"][0]["text"],
            "Keep records for seven years.",
        )

    def test_note_preview_does_not_save_until_confirmed(self) -> None:
        note = {
            "title": "Retention",
            "content": "Keep records for seven years. [1]",
            "sources": ["policy.md"],
        }

        preview = self.client.post("/api/notes", json=note)
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.json()["saved"])
        self.assertFalse(self.notes_dir.exists())

        saved = self.client.post(
            "/api/notes",
            json={**note, "confirmed": True},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["saved"])
        self.assertIn("Keep records for seven years.", saved.json()["preview"])
        self.assertTrue((self.notes_dir / saved.json()["filename"]).is_file())

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
            ["new-info.md", "policy.md"],
        )
        self.assertEqual(
            (self.documents_dir / "new-info.md").read_text(encoding="utf-8"),
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
            (self.documents_dir / "policy.md").read_text(encoding="utf-8"),
            "Keep records for seven years.",
        )

    def test_saved_note_has_download_link_and_download_endpoint(self) -> None:
        note = {
            "title": "Retention",
            "content": "Keep records for seven years. [1]",
            "sources": ["policy.md"],
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


if __name__ == "__main__":
    unittest.main()
