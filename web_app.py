"""FastAPI web interface for the document FAQ agent."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dsl_executor import SUPPORTED_DOCUMENT_SUFFIXES
from faq_agent import DEFAULT_MODEL, FAQAgent


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class SaveNoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    sources: list[str] = Field(default_factory=list, max_length=30)
    confirmed: bool = False


def create_app(agent: FAQAgent | None = None) -> FastAPI:
    """Create the API and frontend app, optionally with an injected agent."""
    faq_agent = agent or FAQAgent(
        documents_dir=Path(
            os.environ.get("FAQ_DOCUMENTS_DIR", str(PROJECT_ROOT / "documents"))
        ),
        notes_dir=Path(os.environ.get("FAQ_NOTES_DIR", str(PROJECT_ROOT / "notes"))),
        model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
    )
    app = FastAPI(title="Paper Trail", version="1.0.0")
    app.state.faq_agent = faq_agent
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def documents_root() -> Path:
        return faq_agent.executor.documents_dir.resolve()

    def notes_root() -> Path:
        return faq_agent.executor.notes_dir.resolve()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/documents")
    def list_documents() -> dict[str, list[str]]:
        try:
            documents = faq_agent.executor.list_docs()
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"documents": [document.name for document in documents]}

    @app.post("/api/documents")
    async def upload_document(file: UploadFile = File(...)) -> dict[str, object]:
        original_name = file.filename or ""
        filename = Path(original_name.replace("\\", "/")).name
        suffix = Path(filename).suffix.casefold()
        if (
            not filename
            or filename in {".", ".."}
            or len(filename) > 255
            or any(character in filename for character in '<>:"|?*')
            or any(ord(character) < 32 for character in filename)
            or filename != filename.rstrip(" .")
        ):
            raise HTTPException(status_code=400, detail="A valid filename is required")
        if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
            raise HTTPException(
                status_code=415,
                detail="Only .md, .rst, and .txt documents are supported",
            )

        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Document exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
            )
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=415, detail="Document must be UTF-8 encoded text"
            ) from exc

        root = documents_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not root.is_dir():
                raise HTTPException(
                    status_code=500, detail="Document directory is unavailable"
                )
            for index in range(1, 10000):
                candidate_name = (
                    filename if index == 1 else f"{Path(filename).stem}-{index}{suffix}"
                )
                destination = root / candidate_name
                try:
                    with destination.open("xb") as destination_file:
                        destination_file.write(data)
                    return {"filename": candidate_name, "size": len(data)}
                except FileExistsError:
                    continue
        except OSError as exc:
            raise HTTPException(status_code=500, detail="Could not store document") from exc
        raise HTTPException(status_code=409, detail="Could not allocate a unique filename")

    @app.post("/api/ask")
    def ask(request: AskRequest) -> dict[str, object]:
        question = request.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="Question must not be blank")
        try:
            result = faq_agent.ask(question)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return {
            "answer": result.answer.text,
            "sources": list(result.answer.sources),
            "citation_warnings": list(result.citation_warnings),
            "evidence": [
                {
                    "source": passage.source,
                    "text": passage.text,
                    "score": passage.score,
                }
                for passage in result.evidence
            ],
        }

    @app.post("/api/notes")
    def save_note(request: SaveNoteRequest) -> dict[str, object]:
        title = request.title.strip()
        content = request.content.strip()
        if not title or not content:
            raise HTTPException(
                status_code=422, detail="Note title and content must not be blank"
            )
        try:
            available_sources = {
                document.name for document in faq_agent.executor.list_docs()
            }
            unknown_sources = sorted(set(request.sources) - available_sources)
            if unknown_sources:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown source document(s): {', '.join(unknown_sources)}",
                )
            result = faq_agent.executor.save_note(
                title,
                content,
                request.sources,
                confirm_callback=lambda _: request.confirmed,
            )
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return {
            "preview": result.preview,
            "saved": result.saved_path is not None,
            "filename": result.saved_path.name if result.saved_path else None,
            "download_url": (
                f"/api/notes/download?{urlencode({'filename': result.saved_path.name})}"
                if result.saved_path
                else None
            ),
        }

    @app.get("/api/notes/download")
    def download_note(
        filename: str = Query(min_length=1, max_length=255),
    ) -> FileResponse:
        if Path(filename).name != filename or not filename.casefold().endswith(".md"):
            raise HTTPException(status_code=404, detail="Note not found")
        root = notes_root()
        note_path = (root / filename).resolve()
        if note_path.parent != root or not note_path.is_file():
            raise HTTPException(status_code=404, detail="Note not found")
        return FileResponse(
            note_path,
            media_type="text/markdown; charset=utf-8",
            filename=note_path.name,
        )

    return app


app = create_app()
