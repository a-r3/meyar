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
- MEYAR is an internal platform: primary surfaces are the internal chat/
  search UI, the CV Library UI, and the internal REST API those UIs (and
  other approved internal systems) call. Don't design routes/schemas
  around an external/commercial customer.
- Local embeddings + pgvector is the approved semantic-search direction
  (see `docs/PROJECT_VISION.md`) — not a forbidden "vector DB" anymore.
  Kafka/Kubernetes/microservices remain unjustified for MVP; installing
  pgvector or any embedding provider still gets its own `DECISIONS.md`
  entry when actually implemented, same as any other new dependency.
- The local-folder CV scanner/indexer is a new ingestion source alongside
  direct upload — it reuses `meyar.ingestion` validation (MIME sniffing,
  size cap, opaque storage id), it doesn't bypass it.
