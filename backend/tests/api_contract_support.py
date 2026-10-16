"""Shared helpers for the issue #45 contract tests: one ``Surface`` abstracts
the two authentication policies (API key vs. session cookie + CSRF) so the same
assertions run against ``/api/v1`` and ``/ui/api/v1``."""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from conftest import BrowserTestClient as AsyncClient

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


@dataclass
class Surface:
    name: str
    base: str
    read_headers: dict[str, str] = field(default_factory=dict)
    write_headers: dict[str, str] = field(default_factory=dict)

    def url(self, path: str) -> str:
        return f"{self.base}{path}"


def machine_surface(plaintext_key: str) -> Surface:
    headers = {"Authorization": f"Bearer {plaintext_key}"}
    return Surface("machine", "/api/v1", dict(headers), dict(headers))


async def human_surface(client: AsyncClient, username: str, password: str) -> Surface:
    login = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert login.status_code == 303
    session = await client.get("/ui/api/v1/session")
    assert session.status_code == 200
    return Surface("human", "/ui/api/v1", {}, {"X-CSRF-Token": session.json()["csrf_token"]})


async def upload_cv(
    client: AsyncClient,
    plaintext_key: str,
    filename: str = "valid_cv.pdf",
    content_type: str = "application/pdf",
) -> tuple[str, str]:
    headers = {"Authorization": f"Bearer {plaintext_key}"}
    created = await client.post("/api/v1/candidates", headers=headers)
    assert created.status_code == 201
    candidate_id = created.json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=headers,
        files={"file": (filename, (FIXTURES_DIR / filename).read_bytes(), content_type)},
    )
    assert upload.status_code == 201
    return candidate_id, upload.json()["id"]


def random_id() -> str:
    return str(uuid.uuid4())
