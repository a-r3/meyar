# MEYAR — API and application contracts (issue #45)

Authority: [DECISIONS.md D-116](DECISIONS.md). OpenAPI (`/openapi.json`, non-production
only) is the canonical machine-readable spec; this page explains the model and the
compatibility rules that a schema cannot.

## Two surfaces, two credential types — never mixed

| | Machine | Human |
|---|---|---|
| Base path | `/api/v1` | `/ui/api/v1` |
| Credential | API key, `Authorization: Bearer <key>` | BrowserSession cookie from the interactive `/ui/login` |
| Mutations | key scope (`jobs:write`, …) | scope **plus** `X-CSRF-Token` header |
| May own a conversation / confirm a JD draft | **no** | yes (accountable `HUMAN_USER`) |
| Auth failure | `401 {"detail": "Invalid or missing API key."}` (legacy body) | `401` error envelope |

Human bootstrap: log in at `/ui/login` (form), then `GET /ui/api/v1/session` returns the
server-derived principal and `csrf_token`; send it as `X-CSRF-Token` on every POST. The
cookie is `Path=/ui`, so it never reaches `/api/v1`. There is no JSON login.

Tenant, user and BrowserSession identity are **never** read from a request. Every request
model forbids unknown fields (`tenant_id`, `user_id`, `session_id` → 422).

## Shared routes (identical on both surfaces)

Defined once in `meyar/api/contracts.py`; they call `meyar/application/*`.

| Route | Notes |
|---|---|
| `GET /candidates` | `page`, `page_size`≤50, `parser_status`, `profile_status`, `folder_status` (closed sets; anything else → 422 `UNSUPPORTED_FILTER`). Newest first, then id. Each item carries `readiness`. |
| `GET /candidates/{id}/detail` | Facts with evidence, `readiness` (+ `retryable_stages`), exact `provenance` (profile version/document/model/prompt). |
| `GET /candidates/{id}/documents/{doc}/original` | Authorized, **audited** attachment `cv-<hex>.pdf|docx`. |
| `POST /candidates/{id}/processing/retry` | `{stage: PROFILE_EXTRACTION\|EMBEDDING, document_id?}`. Only these two stages exist. Local inference only; busy → 503 `RETRYABLE_BUSY`. Scope `candidates:write`. |
| `GET /jobs?status=ACTIVE\|ARCHIVED` | Bounded (`page_size`≤50). |
| `POST /jobs/{id}/archive` | Soft, idempotent (`already_archived`). Criteria and evaluations kept. |
| `GET /jobs/{id}/criteria/{n}/provenance` | Authoring channel, draft link, semantic provenance, non-scorable requirements. |
| `POST /jobs/{id}/criteria/{n}/score` | `candidate_id`, optional `evaluation_as_of_date`, optional `candidate_profile_version_id` (historical, must belong to that candidate). |
| `POST /jobs/{id}/criteria/{n}/rank` | Optional `evaluation_as_of_date`. Each result has the full `ScoreExplanation`. Current effective profiles only. |
| `GET /evaluations/{id}` | Immutable result + full explanation; `UNAVAILABLE` (no score) if its profile is no longer evidence-authorized. |

Machine-only (unchanged): candidate create/delete/upload, `POST /jobs`, criteria versions,
`/search`, `/search/natural-language`, `/usage`, `/health*`.

## Human-only routes (`/ui/api/v1`)

`GET /session`; `GET|POST /conversations`; `GET /conversations/{id}`;
`POST /conversations/{id}/submissions` (single-use identity for one turn);
`POST /conversations/{id}/turns` (`message` ≤4000 chars, `submission_id`, optional
`clarification_id` + `clarification_choice`); `POST /drafts/{id}/resolve`;
`POST /drafts/{id}/confirm`.

A turn returns `200` `COMPLETED` (typed `results`) or `REPLAYED` (same submission again —
never a second turn; read the transcript from the conversation). A turn that did not run
is an error: `409 CONFLICT` (`TURN_IN_PROGRESS`, `STALE`, `CLARIFICATION_NOT_ACTIVE`) or
`503` (`BUSY`, `PROVIDER_FAILURE`, `retryable=true`); nothing is persisted. **A planner
non-executable outcome is a 200 answer**, not an error. Confirmation returns the Job and
criteria-version ids; it never ranks inline (call `rank` afterwards).

## Error contract

```json
{"detail": "The requested resource was not found.",
 "error": {"code": "NOT_FOUND", "reason": null, "message": "…", "retryable": false}}
```

`code` ∈ `AUTHENTICATION_REQUIRED, AUTHORIZATION_DENIED, CSRF_REJECTED, VALIDATION_FAILED,
NOT_FOUND, CONFLICT, RETRYABLE_BUSY, SERVICE_UNAVAILABLE, PAYLOAD_TOO_LARGE,
INTERNAL_ERROR`; `reason` is a closed sub-code (`JOB_ARCHIVED`, `DUPLICATE_ACTIVE_JOB`,
`DRAFT_NOT_ACTIVE`, `UNSUPPORTED_FILTER`, `ORIGINAL_UNAVAILABLE`, …); `message` is fixed
privacy-safe text. Foreign and nonexistent resources are indistinguishable. Pre-existing
machine framework errors (invalid key, generic 422, 500, upload 413) keep their legacy
`{"detail": …}` body; routes moved into the shared factory add `error` and keep the old
`detail` string.

## Compatibility rules (do not change without a reviewed decision)

- **Duplicate jobs:** the human/UI workflow rejects an identical ACTIVE job (409
  `DUPLICATE_ACTIVE_JOB`); `POST /api/v1/jobs` permits duplicates.
- **Evaluation date:** optional everywhere; omitted → configured business "today" resolved
  once per request (`Settings.business_timezone`), echoed as `evaluation_as_of_date` with
  `evaluation_date_defaulted=true`; supplied → used unchanged. Deterministic services never
  read the clock. *Changed in #45:* a missing date was previously a 422.
- **Scoring authority:** deterministic only; no LLM score, no identity input; batch ranking
  = current effective profiles; historical profile versions only for single-candidate scoring.
- **Criteria** are immutable per version; archived jobs cannot be ranked (409 `JOB_ARCHIVED`).

## Original CV access

One operation for the Jinja page, `/api/v1/.../original` and `/ui/api/v1/.../original`:
authorize (tenant-scoped row) → read bytes → record `candidate.original_cv.accessed`
(candidate id, document id, channel `UI|API|UI_JSON`, actor) → return. If the read or the
audit fails, nothing is served (503 `ORIGINAL_UNAVAILABLE`); a denied request records no
success event.

## Known limitations

No JSON login or token auth for humans; no rate limiting (AUD-RATE); legacy machine error
bodies are not yet enveloped; the `ui.service` read-model builders have not been relocated
into `application/`; embedding readiness appears on detail only (not per list item);
identity-extraction retry is not offered; folder reconciliation remains an operator command.
