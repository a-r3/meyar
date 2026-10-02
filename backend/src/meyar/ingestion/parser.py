from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from meyar.ingestion.parser_policy import MAX_DOCX_SOURCE_NODES, MAX_DOCX_TABLE_DEPTH

SourceOrdinal = Annotated[int, Field(strict=True, ge=0, lt=MAX_DOCX_SOURCE_NODES)]


class SourceCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    table: SourceOrdinal
    row: SourceOrdinal
    cell: SourceOrdinal


class BodySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["BODY"] = "BODY"


class TableSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["TABLE"] = "TABLE"
    path: Annotated[list[SourceCell], Field(min_length=1, max_length=MAX_DOCX_TABLE_DEPTH)]
    paragraph: SourceOrdinal


SourceProvenance = Annotated[BodySource | TableSource, Field(discriminator="kind")]
DocxWarning = Literal[
    "DOCX_HEADER_TEXT_OMITTED",
    "DOCX_FOOTER_TEXT_OMITTED",
    "DOCX_TEXTBOX_TEXT_OMITTED",
    "DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED",
]
DOCX_WARNING_CODES: tuple[DocxWarning, ...] = (
    "DOCX_HEADER_TEXT_OMITTED",
    "DOCX_FOOTER_TEXT_OMITTED",
    "DOCX_TEXTBOX_TEXT_OMITTED",
    "DOCX_AMBIGUOUS_MERGE_TEXT_OMITTED",
)


class CanonicalBlock(BaseModel):
    index: int
    text: str
    source: SourceProvenance | None = None


class CanonicalPage(BaseModel):
    page: int
    blocks: list[CanonicalBlock]


class CanonicalDocumentContent(BaseModel):
    """The parser's output shape — deliberately structured (not one giant
    string) so Slice 4 AI evidence can cite a page/block reference back to
    source. See docs/MASTER_SPEC.md §11."""

    language: str | None = None
    pages: list[CanonicalPage]
    warnings: Annotated[list[DocxWarning], Field(max_length=4)] = Field(default_factory=list)


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
    UNSUPPORTED_DOCX_TEXT_ONLY = "UNSUPPORTED_DOCX_TEXT_ONLY"


PARSE_FAILURE_MESSAGES = {
    ParseFailureCode.UNSUPPORTED_DOCX_TEXT_ONLY: (
        "This document contains text in DOCX structures that are not yet supported."
    ),
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

# Only content/explicit output-policy outcomes may become document authority.
# Timeout and memory refusal cannot distinguish hostile content from host/runtime
# conditions in one attempt, so remain operational and retryable too.
TERMINAL_PARSE_FAILURES = frozenset(
    {
        ParseFailureCode.INVALID_DOCUMENT,
        ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT,
        ParseFailureCode.PARSER_OUTPUT_LIMIT,
        ParseFailureCode.UNSUPPORTED_DOCX_TEXT_ONLY,
    }
)


class ParseError(Exception):
    """Raised for any parse failure — malformed content, no extractable
    text, or operational refusal. Only is_terminal outcomes may become durable
    document failure authority; other outcomes must remain retryable. Public
    and persisted metadata must use the fixed server-owned message."""

    def __init__(self, code: ParseFailureCode = ParseFailureCode.INVALID_DOCUMENT) -> None:
        # Legacy/custom providers cannot turn arbitrary exception text into public copy.
        self.code = (
            code if isinstance(code, ParseFailureCode) else ParseFailureCode.INVALID_DOCUMENT
        )
        self.public_message = PARSE_FAILURE_MESSAGES[self.code]
        super().__init__(self.public_message)

    @property
    def is_terminal(self) -> bool:
        return self.code in TERMINAL_PARSE_FAILURES


class DocumentParser(Protocol):
    """Boundary for local, deterministic document parsing. Domain/API code
    must depend only on this Protocol, never on a concrete parser library,
    so Docling (or an OCR-capable parser) can be swapped in later without
    touching callers. See docs/DECISIONS.md D-007."""

    async def parse(self, *, data: bytes, document_type: str) -> ParseResult: ...
