from enum import StrEnum
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


class ParseFailureCode(StrEnum):
    INVALID_DOCUMENT = "INVALID_DOCUMENT"
    PARSER_BUSY = "PARSER_BUSY"
    PARSER_TIMEOUT = "PARSER_TIMEOUT"
    PARSER_RESOURCE_LIMIT = "PARSER_RESOURCE_LIMIT"
    PARSER_RESOURCE_UNAVAILABLE = "PARSER_RESOURCE_UNAVAILABLE"
    PARSER_WORKER_FAILED = "PARSER_WORKER_FAILED"
    PARSER_STARTUP_FAILED = "PARSER_STARTUP_FAILED"
    INVALID_PARSER_OUTPUT = "INVALID_PARSER_OUTPUT"
    INSUFFICIENT_EXTRACTABLE_TEXT = "INSUFFICIENT_EXTRACTABLE_TEXT"
    PARSER_OUTPUT_LIMIT = "PARSER_OUTPUT_LIMIT"


PARSE_FAILURE_MESSAGES = {
    ParseFailureCode.INVALID_DOCUMENT: "The document could not be parsed.",
    ParseFailureCode.PARSER_BUSY: "Document parsing is busy. Please try again later.",
    ParseFailureCode.PARSER_TIMEOUT: "Document parsing exceeded the time limit.",
    ParseFailureCode.PARSER_RESOURCE_LIMIT: "Document parsing exceeded a resource limit.",
    ParseFailureCode.PARSER_RESOURCE_UNAVAILABLE: "Safe document parsing is unavailable.",
    ParseFailureCode.PARSER_WORKER_FAILED: "Document parsing could not be completed.",
    ParseFailureCode.PARSER_STARTUP_FAILED: "Document parsing could not be started.",
    ParseFailureCode.INVALID_PARSER_OUTPUT: "Document parsing returned an invalid result.",
    ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT: "Usable text could not be extracted.",
    ParseFailureCode.PARSER_OUTPUT_LIMIT: "Document parsing exceeded an output limit.",
}


class ParseError(Exception):
    """Raised for any parse failure — malformed content, no extractable
    text, or resource-limit exceeded. Callers must catch this, record safe
    failure metadata, and must not let it crash the request."""

    def __init__(self, code: ParseFailureCode = ParseFailureCode.INVALID_DOCUMENT) -> None:
        # Legacy/custom providers cannot turn arbitrary exception text into public copy.
        self.code = (
            code if isinstance(code, ParseFailureCode) else ParseFailureCode.INVALID_DOCUMENT
        )
        self.public_message = PARSE_FAILURE_MESSAGES[self.code]
        super().__init__(self.public_message)


class DocumentParser(Protocol):
    """Boundary for local, deterministic document parsing. Domain/API code
    must depend only on this Protocol, never on a concrete parser library,
    so Docling (or an OCR-capable parser) can be swapped in later without
    touching callers. See docs/DECISIONS.md D-007."""

    async def parse(self, *, data: bytes, document_type: str) -> ParseResult: ...
