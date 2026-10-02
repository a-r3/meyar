"""Synchronous library calls: import/run only within the isolated worker."""

import io

import docx
import pypdf

from meyar.ingestion.parser import (
    CanonicalBlock,
    CanonicalDocumentContent,
    CanonicalPage,
    DocxWarning,
    ParseError,
    ParseFailureCode,
    ParseResult,
)
from meyar.ingestion.parser_output import TextBudget, validate_result
from meyar.ingestion.parser_policy import PARSER_NAME, PARSER_VERSION, OutputLimits
from meyar.ingestion.parsers.docx_source import extract_body


def parse_sync(data: bytes, kind: str, limits: OutputLimits) -> ParseResult:
    budget = TextBudget(limits)
    pages: list[CanonicalPage] = []
    warnings: list[DocxWarning] = []
    if kind == "PDF":
        reader = pypdf.PdfReader(io.BytesIO(data))
        if len(reader.pages) > limits.pages:
            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            blocks = []
            if text:
                budget.add(text)
                blocks.append(CanonicalBlock(index=0, text=text))
            pages.append(CanonicalPage(page=number, blocks=blocks))
    elif kind == "DOCX":
        document = docx.Document(io.BytesIO(data))
        blocks, warnings = extract_body(document, budget)
        pages.append(CanonicalPage(page=1, blocks=blocks))
    else:
        raise ParseError(ParseFailureCode.INVALID_DOCUMENT)
    result = ParseResult(
        content=CanonicalDocumentContent(pages=pages, warnings=warnings),
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
    validate_result(result, kind, limits)
    return result
