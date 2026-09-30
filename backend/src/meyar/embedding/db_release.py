"""Release the request's DB transaction before a local embedding call
(issue #85, D-089).

Query embeddings are admitted through the shared process-wide inference gate
(meyar.llm.concurrency), so ``embed()`` may WAIT behind LLM work. The
non-agent search routes (``/ui/search``, ``/api/v1/search``,
``/api/v1/search/natural-language``) read candidate profiles before
embedding the query; without this wrapper they would keep a pooled
connection checked out while queued. These routes hold no row locks or
reservation authority, so a plain commit (append-only audit rows at most)
is all that is needed — the agent route uses the stricter
meyar.agent.turn_boundary.BoundaryEmbedding (commit + re-lock/revalidate)."""

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProvider, EmbeddingResult


class DbReleasingEmbeddingProvider:
    def __init__(self, inner: EmbeddingProvider, db: AsyncSession) -> None:
        self._inner = inner
        self._db = db
        self.provider_name = inner.provider_name
        self.model_name = inner.model_name
        self.model_revision = inner.model_revision

    async def embed(self, text: str) -> EmbeddingResult:
        await self._db.commit()
        return await self._inner.embed(text)

    async def health(self) -> dict:
        return await self._inner.health()
