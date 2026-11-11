"""FastAPI web interface for the document FAQ agent."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from ipaddress import ip_address
import os
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from audit_log import AuditLogger
from dsl_executor import SUPPORTED_DOCUMENT_SUFFIXES
from faq_agent import (
    DEFAULT_MODEL,
    FAQAgent,
    OpenRouterRateLimitError,
    Operation,
    OperationExecutionError,
)
from session_store import (
    DEFAULT_MAX_SESSIONS,
    SessionLimitExceeded,
    SessionStore,
    UserSession,
)


PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SESSION_DOCUMENTS = 50
MAX_SESSION_STORAGE_BYTES = 25 * 1024 * 1024
MAX_SESSION_NOTES = 100
MAX_SESSION_NOTE_STORAGE_BYTES = 5 * 1024 * 1024
MAX_ACTIVE_SESSIONS = DEFAULT_MAX_SESSIONS
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
SESSION_COOKIE_NAME = "paper_trail_session"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class SaveNoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    sources: list[str] = Field(default_factory=list, max_length=30)
    confirmed: bool = False


class RequestRateLimiter:
    """In-memory sliding-window limits keyed by client IP and session ID."""

    def __init__(
        self,
        limit: int = RATE_LIMIT_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        max_keys: int = 20_000,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, category: str, identity: str) -> tuple[bool, int]:
        now = time.monotonic()
        key = (category, identity)
        with self._lock:
            timestamps = self._events.get(key)
            if timestamps is not None:
                self._discard_expired(timestamps, now)
                if not timestamps:
                    del self._events[key]
                    timestamps = None

            if timestamps is None:
                if len(self._events) >= self.max_keys:
                    self._discard_all_expired(now)
                if len(self._events) >= self.max_keys:
                    return False, int(self.window_seconds)
                timestamps = deque()
                self._events[key] = timestamps

            if len(timestamps) >= self.limit:
                retry_after = max(
                    1,
                    int(timestamps[0] + self.window_seconds - now + 0.999),
                )
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def _discard_expired(self, timestamps: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def _discard_all_expired(self, now: float) -> None:
        for key, timestamps in list(self._events.items()):
            self._discard_expired(timestamps, now)
            if not timestamps:
                del self._events[key]


def create_app(
    agent: FAQAgent | None = None,
    audit_logger: AuditLogger | None = None,
    sample_documents_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> FastAPI:
    """Create the API and frontend app, optionally with an injected agent."""
    idle_timeout_seconds = float(os.environ.get("FAQ_SESSION_IDLE_HOURS", "3")) * 60 * 60
    store = SessionStore(
        data_dir=data_dir
        or os.environ.get("FAQ_DATA_DIR", str(PROJECT_ROOT / "data")),
        sample_documents_dir=sample_documents_dir or PROJECT_ROOT / "documents",
        idle_timeout_seconds=idle_timeout_seconds,
        max_sessions=MAX_ACTIVE_SESSIONS,
    )
    cleanup_interval_seconds = max(
        60.0, min(idle_timeout_seconds / 4, 15 * 60)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async def cleanup_loop() -> None:
            await asyncio.to_thread(store.cleanup_idle)
            while True:
                await asyncio.sleep(cleanup_interval_seconds)
                await asyncio.to_thread(store.cleanup_idle)

        cleanup_task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task

    audit_path = Path(
        os.environ.get(
            "FAQ_AUDIT_LOG",
            str(PROJECT_ROOT / "logs" / "audit.jsonl"),
        )
    )
    audit = audit_logger or AuditLogger(audit_path)
    app = FastAPI(title="Paper Trail", version="1.0.0", lifespan=lifespan)
    app.state.agent_prototype = agent
    app.state.session_store = store
    app.state.audit_logger = audit
    app.state.note_quota_lock = threading.Lock()
    app.state.rate_limiter = RequestRateLimiter(
        limit=RATE_LIMIT_REQUESTS,
        window_seconds=RATE_LIMIT_WINDOW_SECONDS,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def session_agent(session: UserSession) -> FAQAgent:
        prototype = app.state.agent_prototype
        return FAQAgent(
            documents_dir=session.documents_dir,
            notes_dir=session.notes_dir,
            model=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
            llm_call=prototype.llm_call if prototype is not None else None,
            audit_logger=audit,
        )

    @app.middleware("http")
    async def audit_requests(request, call_next):
        path = request.url.path
        if path in {"/", "/healthz"} or path == "/static" or path.startswith(
            "/static/"
        ):
            return await call_next(request)

        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        session_cookie = _valid_session_id_cookie(
            request.cookies.get(SESSION_COOKIE_NAME)
        )
        context_token = audit.set_context(request_id, session_cookie)
        try:
            response = await _handle_audited_request(request, call_next, path)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            audit.reset_context(context_token)

    async def _handle_audited_request(request, call_next, path: str):
        started = time.monotonic()
        status_code = 500
        outcome = "failure"
        limiter: RequestRateLimiter = app.state.rate_limiter
        if path.startswith("/api/"):
            client_ip = _client_ip(request)
            allowed, retry_after = limiter.allow("ip", client_ip)
            if not allowed:
                await asyncio.to_thread(
                    audit.log,
                    "http.request",
                    "rate_limited",
                    {
                        "method": request.method,
                        "path": path,
                        "status_code": 429,
                        "limit_scope": "ip",
                    },
                )
                return _rate_limited_response(retry_after)

        try:
            session = await asyncio.to_thread(
                store.open_session,
                request.cookies.get(SESSION_COOKIE_NAME),
            )
        except SessionLimitExceeded as exc:
            await asyncio.to_thread(
                audit.log,
                "http.request",
                "failure",
                {
                    "method": request.method,
                    "path": path,
                    "status_code": 503,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            return JSONResponse(status_code=503, content={"detail": str(exc)})

        session_context_token = audit.set_context(
            request.state.request_id,
            session.session_id,
        )
        try:
            return await _handle_session_request(
                request,
                call_next,
                path,
                session,
                started,
                status_code,
                outcome,
                idle_timeout_seconds,
                audit,
                store,
                limiter,
            )
        finally:
            audit.reset_context(session_context_token)

    async def _handle_session_request(
        request,
        call_next,
        path: str,
        session: UserSession,
        started: float,
        status_code: int,
        outcome: str,
        idle_timeout: float,
        audit_logger: AuditLogger,
        session_store: SessionStore,
        limiter: RequestRateLimiter,
    ):
        if path.startswith("/api/"):
            allowed, retry_after = limiter.allow("session", session.session_id)
            if not allowed:
                response = _rate_limited_response(retry_after)
                response.set_cookie(
                    SESSION_COOKIE_NAME,
                    session.session_id,
                    max_age=int(idle_timeout),
                    httponly=True,
                    samesite="lax",
                    secure=os.environ.get("FAQ_COOKIE_SECURE", "false").casefold()
                    in {"1", "true", "yes"},
                    path="/",
                )
                status_code = 429
                outcome = "rate_limited"
                await asyncio.to_thread(
                    audit_logger.log,
                    "http.request",
                    outcome,
                    {
                        "method": request.method,
                        "path": path,
                        "status_code": status_code,
                        "limit_scope": "session",
                    },
                )
                await asyncio.to_thread(session_store.touch, session)
                return response

        request.state.user_session = session
        try:
            response = await call_next(request)
            status_code = response.status_code
            outcome = "success" if status_code < 400 else "failure"
            response.set_cookie(
                SESSION_COOKIE_NAME,
                session.session_id,
                max_age=int(idle_timeout),
                httponly=True,
                samesite="lax",
                secure=os.environ.get("FAQ_COOKIE_SECURE", "false").casefold()
                in {"1", "true", "yes"},
                path="/",
            )
            return response
        finally:
            await asyncio.to_thread(session_store.touch, session)
            await asyncio.to_thread(
                audit_logger.log,
                "http.request",
                outcome,
                {
                    "method": request.method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )

    def documents_root(session: UserSession) -> Path:
        return session.documents_dir.resolve()

    def notes_root(session: UserSession) -> Path:
        return session.notes_dir.resolve()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz", include_in_schema=False, response_class=Response)
    def health_check() -> Response:
        return Response(content="ok", media_type="text/plain")

    @app.get("/api/documents")
    def list_documents(request: Request) -> dict[str, list[str]]:
        faq_agent = session_agent(request.state.user_session)
        try:
            documents = faq_agent.executor.list_docs()
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"documents": [document.name for document in documents]}

    @app.post("/api/documents")
    async def upload_document(
        request: Request,
        file: UploadFile = File(...),
    ) -> dict[str, object]:
        faq_agent = session_agent(request.state.user_session)
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

        root = documents_root(request.state.user_session)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not root.is_dir():
                raise HTTPException(
                    status_code=500, detail="Document directory is unavailable"
                )
            current_documents = faq_agent.executor.list_docs()
            if len(current_documents) >= MAX_SESSION_DOCUMENTS:
                raise HTTPException(
                    status_code=413,
                    detail=f"Session document limit is {MAX_SESSION_DOCUMENTS} files",
                )
            current_size = sum(
                (root / document.name).stat().st_size
                for document in current_documents
            )
            if current_size + len(data) > MAX_SESSION_STORAGE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Session document storage limit is 25 MB",
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

    @app.delete("/api/documents/{filename}", status_code=204)
    def delete_document(request: Request, filename: str) -> Response:
        faq_agent = session_agent(request.state.user_session)
        if (
            not filename
            or Path(filename.replace("\\", "/")).name != filename
            or Path(filename).suffix.casefold() not in SUPPORTED_DOCUMENT_SUFFIXES
        ):
            raise HTTPException(status_code=404, detail="Document not found")

        root = documents_root(request.state.user_session)
        document_path = (root / filename).resolve()
        if document_path.parent != root or not document_path.is_file():
            audit.log("document.delete", "not_found")
            raise HTTPException(status_code=404, detail="Document not found")
        try:
            document_path.unlink()
        except OSError as exc:
            audit.log("document.delete", "failure")
            raise HTTPException(status_code=500, detail="Could not remove document") from exc
        audit.log("document.delete", "deleted")
        return Response(status_code=204)

    @app.post("/api/ask", response_model=None)
    def ask(request: Request, body: AskRequest) -> dict[str, object] | JSONResponse:
        faq_agent = session_agent(request.state.user_session)
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="Question must not be blank")
        started = time.perf_counter()
        try:
            result = faq_agent.ask(question)
        except OperationExecutionError as exc:
            if isinstance(exc.__cause__, OpenRouterRateLimitError):
                return _upstream_rate_limit_response()
            elapsed_ms = (time.perf_counter() - started) * 1000
            return JSONResponse(
                status_code=502,
                content={
                    "detail": str(exc.__cause__ or exc),
                    "operations": [_operation_payload(item) for item in exc.operations],
                    "timings": {
                        "planning_ms": round(exc.planning_duration_ms, 2),
                        "execution_ms": round(
                            sum(item.duration_ms for item in exc.operations), 2
                        ),
                        "total_ms": round(elapsed_ms, 2),
                    },
                },
            )
        except OpenRouterRateLimitError:
            return _upstream_rate_limit_response()
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        operations = [_operation_payload(item) for item in result.operations]
        execution_ms = sum(item.duration_ms for item in result.operations)
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
            "operations": operations,
            "timings": {
                "planning_ms": round(result.planning_duration_ms, 2),
                "execution_ms": round(execution_ms, 2),
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        }

    @app.post("/api/notes")
    def save_note(request: Request, body: SaveNoteRequest) -> dict[str, object]:
        faq_agent = session_agent(request.state.user_session)
        title = body.title.strip()
        content = body.content.strip()
        if not title or not content:
            raise HTTPException(
                status_code=422, detail="Note title and content must not be blank"
            )
        try:
            available_sources = {
                document.name for document in faq_agent.executor.list_docs()
            }
            unknown_sources = sorted(set(body.sources) - available_sources)
            if unknown_sources:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown source document(s): {', '.join(unknown_sources)}",
                )
            if body.confirmed:
                preview_size = _note_preview_size(title, content, body.sources)
                with app.state.note_quota_lock:
                    note_count, note_size = _session_note_usage(notes_root(request.state.user_session))
                    if note_count >= MAX_SESSION_NOTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Session note limit reached ({MAX_SESSION_NOTES} notes)",
                        )
                    if note_size + preview_size > MAX_SESSION_NOTE_STORAGE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="Session note storage limit is 5 MB",
                        )
                    result = faq_agent.executor.save_note(
                        title,
                        content,
                        body.sources,
                        confirm_callback=lambda _: True,
                    )
            else:
                result = faq_agent.executor.save_note(
                    title,
                    content,
                    body.sources,
                    confirm_callback=lambda _: False,
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
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
    ) -> FileResponse:
        if Path(filename).name != filename or not filename.casefold().endswith(".md"):
            raise HTTPException(status_code=404, detail="Note not found")
        root = notes_root(request.state.user_session)
        note_path = (root / filename).resolve()
        if note_path.parent != root or not note_path.is_file():
            raise HTTPException(status_code=404, detail="Note not found")
        return FileResponse(
            note_path,
            media_type="text/markdown; charset=utf-8",
            filename=note_path.name,
        )

    return app


def _operation_payload(operation: Operation) -> dict[str, object]:
    """Expose action status without returning potentially sensitive arguments."""
    return {
        "command": operation.command,
        "status": operation.status,
        "feedback": operation.feedback,
        "duration_ms": round(operation.duration_ms, 2),
    }


def _session_note_usage(notes_dir: Path) -> tuple[int, int]:
    """Return regular-file count and byte size without following symlinks."""
    if not notes_dir.is_dir():
        return 0, 0
    count = 0
    total_bytes = 0
    for path in notes_dir.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        total_bytes += path.stat().st_size
    return count, total_bytes


def _note_preview_size(title: str, content: str, sources: list[str]) -> int:
    source_names = list(dict.fromkeys(source.strip() for source in sources))
    source_list = "\n".join(f"- {source}" for source in source_names) or "- None"
    preview = f"# {title}\n\n{content}\n\nSources:\n{source_list}\n"
    return len(preview.encode("utf-8"))


def _client_ip(request: Request) -> str:
    """Use the first forwarded address, falling back to the socket peer."""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        first_address = forwarded_for.split(",", 1)[0].strip()
        try:
            return str(ip_address(first_address))
        except ValueError:
            pass
    if request.client is not None:
        try:
            return str(ip_address(request.client.host))
        except ValueError:
            return request.client.host
    return "unknown"


def _valid_session_id_cookie(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _rate_limited_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": "You're making requests too quickly. Please wait a moment and try again."
        },
        headers={"Retry-After": str(retry_after)},
    )


def _upstream_rate_limit_response() -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": "The AI service is busy right now. Please wait a moment and try again."},
        headers={"Retry-After": "30"},
    )


app = create_app()
