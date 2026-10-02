"""Issue #46 PR-1 — candidate-document request-envelope bound. Synthetic
bytes only."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from meyar.api.body_limit import MAX_MULTIPART_OVERHEAD_BYTES, DocumentUploadBodyLimitMiddleware
from meyar.config import Settings, get_settings
from meyar.ingestion.bounded_read import read_bounded
from meyar.ingestion.validation import too_large_message

MAX_BYTES = 1000
BOUND = MAX_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
UPLOAD_PATH = "/api/v1/candidates/00000000-0000-0000-0000-000000000001/documents"


class _Harness:
    """Drives the pure ASGI middleware directly, counting what is received."""

    def __init__(self, chunks: list[bytes], *, content_length: str | None) -> None:
        self.chunks = list(chunks)
        self.real_receive_calls = 0
        self.app_called = False
        self.app_received = 0
        self.sent: list[dict[str, Any]] = []
        headers = [(b"content-type", b"multipart/form-data; boundary=x")]
        if content_length is not None:
            headers.append((b"content-length", content_length.encode()))
        self.scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": UPLOAD_PATH,
            "headers": headers,
            "app": SimpleNamespace(
                dependency_overrides={get_settings: lambda: Settings(max_upload_bytes=MAX_BYTES)}
            ),
        }

    async def receive(self) -> dict[str, Any]:
        self.real_receive_calls += 1
        if not self.chunks:
            return {"type": "http.disconnect"}
        body = self.chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def downstream(self, scope: Any, receive: Any, send: Any) -> None:
        # Stands in for FastAPI: parse the body, then answer 400 if the
        # connection dropped mid-body (as its multipart error path does).
        self.app_called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"parse error"})
                return
            self.app_received += len(message.get("body", b""))
            if not message.get("more_body"):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})
                return

    async def run(self) -> None:
        await DocumentUploadBodyLimitMiddleware(self.downstream)(
            self.scope, self.receive, self.send
        )

    @property
    def statuses(self) -> list[int]:
        return [m["status"] for m in self.sent if m["type"] == "http.response.start"]

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


async def test_honest_over_limit_content_length_rejected_without_reading() -> None:
    harness = _Harness([b"x" * 10], content_length=str(BOUND + 1))
    await harness.run()
    assert harness.statuses == [413]
    assert json.loads(harness.body()) == {"detail": too_large_message(MAX_BYTES)}
    assert harness.app_called is False
    assert harness.real_receive_calls == 0


async def test_streamed_body_without_content_length_stops_at_bound() -> None:
    chunk = 30_000
    harness = _Harness([b"x" * chunk] * 20, content_length=None)
    await harness.run()
    assert harness.statuses == [413]  # downstream's own 400 is suppressed
    assert json.loads(harness.body()) == {"detail": too_large_message(MAX_BYTES)}
    # 66_536-byte bound crossed on the 3rd chunk; nothing further is read.
    assert harness.real_receive_calls == 3
    assert harness.app_received < BOUND


async def test_understated_content_length_cannot_bypass_streaming_bound() -> None:
    harness = _Harness([b"x" * 30_000] * 20, content_length="100")
    await harness.run()
    assert harness.statuses == [413]
    assert harness.real_receive_calls == 3


async def test_invalid_content_length_is_ignored_and_streaming_counts() -> None:
    harness = _Harness([b"x" * 30_000] * 20, content_length="not-a-number")
    await harness.run()
    assert harness.statuses == [413]


async def test_body_exactly_at_bound_passes_through() -> None:
    harness = _Harness([b"x" * BOUND], content_length=str(BOUND))
    await harness.run()
    assert harness.statuses == [200]
    assert harness.app_received == BOUND


async def test_other_routes_and_methods_are_not_limited() -> None:
    for method, path in (("POST", "/api/v1/jobs"), ("GET", UPLOAD_PATH), ("PUT", UPLOAD_PATH)):
        harness = _Harness([b"x" * (BOUND * 3)], content_length=str(BOUND * 3))
        harness.scope["method"], harness.scope["path"] = method, path
        await harness.run()
        assert harness.statuses == [200], (method, path)


async def test_non_http_scopes_pass_through() -> None:
    called: list[str] = []

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    await DocumentUploadBodyLimitMiddleware(downstream)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert called == ["lifespan"]


# --- through the real application stack ---------------------------------


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


async def _candidate(client: AsyncClient, plaintext: str) -> str:
    resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.fixture
def small_limit():
    from meyar.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(max_upload_bytes=MAX_BYTES)
    yield
    app.dependency_overrides.pop(get_settings, None)


async def test_real_app_over_limit_body_never_reaches_handler(
    client: AsyncClient, tenant_and_key, small_limit, monkeypatch: pytest.MonkeyPatch
) -> None:
    _t, _k, plaintext = tenant_and_key
    candidate_id = await _candidate(client, plaintext)

    async def forbidden(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("handler must not run for an over-bound request")

    monkeypatch.setattr("meyar.api.v1.candidates.read_bounded", forbidden)
    resp = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={"file": ("cv.pdf", b"%PDF-" + b"x" * (BOUND + 10), "application/pdf")},
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": too_large_message(MAX_BYTES)}


async def test_real_app_file_exactly_at_limit_passes_transport_to_validation(
    client: AsyncClient, tenant_and_key, small_limit
) -> None:
    _t, _k, plaintext = tenant_and_key
    candidate_id = await _candidate(client, plaintext)
    resp = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={"file": ("cv.pdf", b"x" * MAX_BYTES, "application/pdf")},
    )
    # Not stopped by the envelope guard: reaches application validation,
    # which rejects the synthetic non-PDF bytes as unsupported (422).
    assert resp.status_code == 422


async def test_real_app_file_one_byte_over_limit_rejected_by_validator(
    client: AsyncClient, tenant_and_key, small_limit
) -> None:
    _t, _k, plaintext = tenant_and_key
    candidate_id = await _candidate(client, plaintext)
    resp = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={"file": ("cv.pdf", b"x" * (MAX_BYTES + 1), "application/pdf")},
    )
    assert resp.status_code == 413
    assert resp.json() == {"detail": too_large_message(MAX_BYTES)}


# --- handler-side bounded read -------------------------------------------


class _RecordingSource:
    def __init__(self, size: int) -> None:
        self.remaining = size
        self.requested = 0
        self.delivered = 0

    async def read(self, size: int = -1, /) -> bytes:
        assert size > 0, "unbounded read requested"
        self.requested += size
        take = min(size, self.remaining)
        self.remaining -= take
        self.delivered += take
        return b"x" * take


async def test_read_bounded_stops_at_max_plus_one() -> None:
    source = _RecordingSource(10_000_000)
    data = await read_bounded(source, max_bytes=100_000)
    assert len(data) == 100_001
    assert source.delivered == 100_001
    assert source.requested == 100_001


async def test_read_bounded_returns_small_files_whole() -> None:
    source = _RecordingSource(500)
    assert len(await read_bounded(source, max_bytes=1000)) == 500


def test_upload_route_has_no_unbounded_read() -> None:
    source = (
        Path(__file__).resolve().parent.parent / "src/meyar/api/v1/candidates.py"
    ).read_text()
    assert "await file.read()" not in source
    assert "read_bounded(file" in source
