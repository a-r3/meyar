---
name: security-review
description: Review changes for tenant leaks, auth failures, PII leaks, prompt injection, unsafe uploads, secret exposure. Use before closing a slice that touches auth, tenant data, uploads, or LLM prompts.
---

Inspect the diff (not the whole repo) for:
1. Tenant isolation — every new/changed query on a tenant-owned table
   scoped by `tenant_id`.
2. Auth — every new route requiring auth actually has the auth dependency;
   scopes match the route's intent.
3. PII — no CV text/name/email/phone/secret passed to a logger.
4. Prompt injection — any new prompt treats CV text as quoted data only;
   any new LLM call output is schema-validated before persistence.
5. Uploads — MIME sniffed, size capped, opaque storage id, no filename used
   as a path.
6. Secrets — no plaintext API key persisted/logged; no `.env`/credential
   committed.

Report blocking vs. non-blocking findings. Fix blocking findings
immediately; log non-blocking ones in `docs/DECISIONS.md` or
`docs/STATUS.md` and continue — do not iterate the review on unchanged code.
