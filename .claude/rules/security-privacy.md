# Security & privacy rules

- Every repository function touching a tenant-owned table requires
  `tenant_id` as an explicit parameter and filters on it. No exceptions.
- API key secrets: generate with `secrets.token_urlsafe`, store only a
  SHA-256 hash, return the plaintext exactly once at creation. Never log an
  API key (full or partial secret) — only the stored prefix is safe to log.
- Any log call must carry only ids/enums/durations. Never pass CV text,
  candidate name/email/phone, or full request/response bodies to a logger.
- Any LLM output must be parsed through a Pydantic v2 model before it is
  used for persistence or returned to an authorized internal user/system. A validation failure is a
  handled outcome (`MANUAL_REVIEW_REQUIRED`/`FAILED`), never a silent
  coercion.
- CV text passed into a prompt must be clearly delimited as quoted data; the
  prompt/schema must not expose any field the model could use as an
  "instruction override."
- Uploaded files: verify MIME by content sniffing (not filename), enforce
  max size, generate an opaque storage id, never use the original filename
  as or in a filesystem path.
- Never commit real candidate CVs, `.env`, credentials, or key material.
- Files discovered by the local folder scanner/indexer are untrusted input
  exactly like a direct upload — same MIME sniffing, size cap, opaque
  storage id, and no filename-derived paths. A local file is not
  implicitly more trusted than an uploaded one.
- Embedding generation must go through a local provider abstraction (same
  boundary pattern as `LLMProvider`) — never send candidate profile text
  or vectors to an external embedding API.
- Any UI/API surface that exposes original CV bytes or `CandidateIdentity`
  fields (name/contact) requires the same authenticated/authorized access
  control as the rest of the API — no anonymous/public read path.
  `CandidateIdentity` may be displayed to authorized users but must never
  be read by the matching/search/ranking engine as a signal.
