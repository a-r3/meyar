# Architecture rules

- Modular monolith: one FastAPI app + one in-process async worker. No new
  services/processes without a DECISIONS.md entry justifying it.
- Layering: `api/` (routers, request/response only) → `services/` (use
  cases) → `models/` (SQLAlchemy) via repository-style functions that always
  take `tenant_id` explicitly. Routers never build raw SQL/ORM queries
  directly against tenant tables.
- `meyar.llm.LLMProvider` is the only allowed boundary to Ollama. No
  `import ollama` outside `meyar/llm/`.
- All cross-service/module contracts (queue, LLM provider, storage) are
  Python protocols/ABCs so implementations are swappable without touching
  callers.
- Every new tenant-owned table gets `tenant_id` as a non-null FK from
  creation — not added later.
- OpenAPI (FastAPI-generated) is the canonical API spec. Don't hand-write a
  duplicate spec.
