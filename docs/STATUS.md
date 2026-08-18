# MEYAR — Status

## Current phase
Slice 3 (Candidate Upload / Secure Document Ingestion) implemented and
verified. Not yet committed.

## Completed
- Preflight, fast docs pass, Claude Code harness.
- Backend scaffold: FastAPI + SQLAlchemy 2.0 async + Alembic + PostgreSQL.
- Slice 1 (Tenant + API Auth) — committed as `f5b4ec3`.
- Slice 2 (Job Criteria) — committed as `9ef3273`.
- Slice 3 (Candidate Upload): `Candidate` (minimal, no PII fields yet),
  `CandidateDocument` (metadata: original_filename, mime_type, byte_size,
  sha256_hash, opaque storage_key, document_status, parser_status +
  error fields), `CanonicalDocument` (parser output: pages/blocks JSON,
  parser_name/version, language) — separate table so reprocessing never
  overwrites prior provenance. `DocumentStorage` protocol +
  `LocalFilesystemStorage` (opaque `{tenant_id}/{uuid4}` keys, atomic
  writes, no filename ever used as a path). `DocumentParser` protocol +
  `LocalTextParser` (pypdf + python-docx, no OCR, 300-page cap) — Docling
  deferred, see D-007. Upload validation cross-checks extension + declared
  content-type + actual file signature (DOCX validated as real OOXML, not
  just any zip). Endpoints: `POST/GET /v1/candidates`, `DELETE
  /v1/candidates/{id}` (hard-delete cascade: files then DB rows, before
  audit event), `POST/GET /v1/candidates/{id}/documents`, `GET
  .../documents/{document_id}` — all tenant-scoped (404 on cross-tenant),
  scoped by `candidates:read`/`candidates:write` (already-present
  scopes). Audit events: CANDIDATE_CREATED, CANDIDATE_DOCUMENT_UPLOADED,
  CANDIDATE_DOCUMENT_PARSED, CANDIDATE_DOCUMENT_PARSE_FAILED,
  CANDIDATE_DELETED. No LLM/Ollama call anywhere in this slice.

## Tests
38/38 passing (19 prior + 19 new: candidate CRUD, scope enforcement,
valid PDF/DOCX parse, prompt-injection fixture parsed as inert data,
unsupported type/signature rejection, malformed PDF → upload succeeds but
PARSE_FAILED (not a crash), malformed DOCX → rejected at validation,
extension/content mismatch rejection, oversized rejection, path-traversal
filename harmlessness, list/get metadata, delete cascade removes both
files and rows, cross-tenant isolation across all candidate/document
routes, PII-not-in-logs regression guard). `ruff` and `mypy` clean. Live
smoke flow verified manually against a running server: create candidate →
upload synthetic PDF → parsed synchronously → canonical text confirmed
present and correct → metadata retrieval → unauthenticated/unsupported
requests correctly rejected → storage layout on disk confirmed opaque
(`{tenant_id}/{uuid4}`, no original filename).

## In progress
Nothing in flight.

## Blockers
None blocking. Same open items as before: real Apple Silicon Mac not yet
available (D-001); Auto Mode script still dry-run only. New: encryption-
at-rest for stored documents is explicitly deferred to host/disk-level
protection for MVP (see SECURITY_PRIVACY.md "Document storage") — must be
addressed before real production candidate data is handled.

## Next slice
Slice 4 — Local AI Candidate Profile Extraction (consumes the
CanonicalDocument built here). Slice 3 work is implemented/tested but
intentionally left uncommitted per current instructions.
