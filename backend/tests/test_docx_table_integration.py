"""PR-3 upload/API/UI/history contract using synthetic DOCX source only."""

import uuid

import pytest
from sqlalchemy import select
from test_docx_review_corrections import omitted
from test_docx_tables import row_document, save, textbox
from test_parser_isolation import VALID

from meyar.config import Settings, get_settings
from meyar.ingestion.parser import ParseError, ParseFailureCode
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.candidate_document import CandidateDocument
from meyar.models.canonical_document import CanonicalDocument
from meyar.services.candidate_profile_repo import create_profile_version
from meyar.services.folder_indexer_service import index_folder
from meyar.storage.local import LocalFilesystemStorage


@pytest.mark.parametrize(
    "mode", ["table", "mixed_header", "mixed_footer", "mixed_box", "mixed_sdt", "unsupported"]
)
async def test_upload_api_original_ui_partial_and_terminal(
    client, db_session, tenant_key_and_user, mode
):
    import docx

    tenant, _, key, user, password, _ = tenant_key_and_user
    headers = {"Authorization": f"Bearer {key}"}
    document = docx.Document() if mode == "unsupported" else row_document("Python")
    if mode in ("mixed_header", "unsupported"):
        document.sections[0].header.paragraphs[0].text = "OMITTED SYNTHETIC HEADER"
    if mode == "mixed_footer":
        document.sections[0].footer.paragraphs[0].text = "OMITTED SYNTHETIC FOOTER"
    if mode == "mixed_box":
        textbox(document, "OMITTED SYNTHETIC BOX")
    if mode == "mixed_sdt":
        omitted(document.tables[0].add_row().cells[0], "sdt", "OMITTED SYNTHETIC CONTROL")
    data = save(document)
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{candidate}/documents",
        headers=headers,
        files={
            "file": (
                "synthetic.docx",
                data,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert upload.status_code == 201
    body = upload.json()
    canonical = await db_session.scalar(select(CanonicalDocument))
    if mode == "unsupported":
        assert body["parser_status"] == "PARSE_FAILED" and body["canonical"] is None
        assert canonical is None
        assert body["parse_error_code"] == "UNSUPPORTED_DOCX_TEXT_ONLY"
        assert body["parse_error_message"] == (
            "This document contains text in DOCX structures that are not yet supported."
        )
    else:
        assert body["parser_status"] == "PARSED" and body["parser_version"] == "1.2.0"
        assert body["canonical"]["partial_extraction"] == (mode != "table")
        assert "source" not in body["canonical"]["pages"][0]["blocks"][0]
        assert "warnings" not in body["canonical"]
        assert "OMITTED SYNTHETIC" not in upload.text
        assert "row_context_complete" not in upload.text
        assert canonical.content["pages"][0]["blocks"][0]["source"]["kind"] == "TABLE"
        document_row = await db_session.get(CandidateDocument, uuid.UUID(body["id"]))
        await create_profile_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=uuid.UUID(candidate),
            candidate_document_id=document_row.id,
            canonical_document_id=canonical.id,
            source_sha256=document_row.sha256_hash,
            schema_version="candidate-profile-v1",
            prompt_version="synthetic",
            model_provider="fake",
            model_name="fake",
            model_metadata={},
            status="COMPLETED",
            profile_content={
                "skills": [
                    {
                        "name": "Python",
                        "evidence": [{"page": 1, "block_index": 0, "quote": "Python"}],
                    }
                ],
            },
        )
    original_url = f"/ui/candidates/{candidate}/documents/{body['id']}/original"
    assert (await client.get(original_url)).status_code == 303
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    assert (
        await client.post("/ui/login", data={"username": user.username, "password": password})
    ).status_code == 303
    original = await client.get(original_url)
    assert original.status_code == 200 and original.content == data
    preview = await client.get(f"/ui/candidates/{candidate}/documents/{body['id']}/preview")
    detail = await client.get(f"/ui/candidates/{candidate}")
    assert preview.status_code == 200 and detail.status_code == 200
    for response in (preview, detail):
        assert "OMITTED SYNTHETIC" not in response.text
        assert "DOCX_HEADER_TEXT_OMITTED" not in response.text
        assert "DOCX_FOOTER_TEXT_OMITTED" not in response.text
        assert "DOCX_TEXTBOX_TEXT_OMITTED" not in response.text
        assert "DOCX_TABLE_TEXT_OMITTED" not in response.text
        assert "row_context_complete" not in response.text
        assert "UNSUPPORTED_DOCX_TEXT_ONLY" not in response.text
        assert '"path"' not in response.text and '"cell"' not in response.text
        assert "səhifə 1" not in response.text and "Səhifə 1" not in response.text
    if mode.startswith("mixed"):
        assert (
            "Orijinal CV-ni yoxlayın" in preview.text and "Orijinal CV-ni yoxlayın" in detail.text
        )
    if mode != "unsupported":
        assert "CV-də — “Python”" in detail.text
        before = canonical.content.copy()
        canonical.parser_version = "1.1.0"
        await db_session.flush()
        assert (await client.get(f"/ui/candidates/{candidate}")).status_code == 200
        assert canonical.content == before and canonical.parser_version == "1.1.0"


async def test_pdf_physical_evidence_page_wording_remains(client, db_session, tenant_key_and_user):
    tenant, _, key, user, password, _ = tenant_key_and_user
    headers = {"Authorization": f"Bearer {key}"}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{candidate}/documents",
        headers=headers,
        files={"file": ("synthetic.pdf", VALID, "application/pdf")},
    )
    document = await db_session.get(CandidateDocument, uuid.UUID(upload.json()["id"]))
    canonical = await db_session.scalar(select(CanonicalDocument))
    text = canonical.content["pages"][0]["blocks"][0]["text"]
    await create_profile_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate),
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256=document.sha256_hash,
        schema_version="candidate-profile-v1",
        prompt_version="synthetic",
        model_provider="fake",
        model_name="fake",
        model_metadata={},
        status="COMPLETED",
        profile_content={
            "skills": [
                {"name": "Python", "evidence": [{"page": 1, "block_index": 0, "quote": text}]}
            ],
        },
    )
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    await client.post("/ui/login", data={"username": user.username, "password": password})
    detail = await client.get(f"/ui/candidates/{candidate}")
    assert "CV, səhifə 1" in detail.text


async def test_historical_table_only_failure_not_backfilled_on_parser_version_change(
    db_session, tenant_and_key, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "table.docx").write_bytes(save(row_document("Python")))
    parser = LocalTextParser()
    actual = parser.parse

    async def historical_failure(**kwargs):
        raise ParseError(ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT)

    monkeypatch.setattr(parser, "parse", historical_failure)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    kwargs = dict(tenant_id=tenant.id, root_path=str(root), max_bytes=10 * 1024 * 1024)
    first = await index_folder(db_session, storage, parser, **kwargs)
    assert first.successful == 1
    monkeypatch.setattr(parser, "parse", actual)
    again = await index_folder(db_session, storage, parser, **kwargs)
    assert again.unchanged == 1 and again.successful == 0
    assert await db_session.scalar(select(CanonicalDocument)) is None
    document = await db_session.scalar(select(CandidateDocument))
    assert document.parser_status == "PARSE_FAILED"
    assert document.parse_error_code == "INSUFFICIENT_EXTRACTABLE_TEXT"
