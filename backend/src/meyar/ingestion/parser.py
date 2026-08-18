from typing import Protocol

from pydantic import BaseModel


class CanonicalBlock(BaseModel):
    index: int
    text: str


class CanonicalPage(BaseModel):
    page: int
    blocks: list[CanonicalBlock]


class CanonicalDocumentContent(BaseModel):
    """The parser's output shape — deliberately structured (not one giant
    string) so Slice 4 AI evidence can cite a page/block reference back to
    source. See docs/MASTER_SPEC.md §11."""

    language: str | None = None
    pages: list[CanonicalPage]


class ParseResult(BaseModel):
    content: CanonicalDocumentContent
    parser_name: str
    parser_version: str


class ParseError(Exception):
    """Raised for any parse failure — malformed content, no extractable
    text, or resource-limit exceeded. Callers must catch this, record safe
    failure metadata, and must not let it crash the request."""


class DocumentParser(Protocol):
    """Boundary for local, deterministic document parsing. Domain/API code
    must depend only on this Protocol, never on a concrete parser library,
    so Docling (or an OCR-capable parser) can be swapped in later without
    touching callers. See docs/DECISIONS.md D-007."""

    async def parse(self, *, data: bytes, document_type: str) -> ParseResult: ...
