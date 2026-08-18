import io

import docx
import pypdf

from meyar.ingestion.parser import (
    CanonicalBlock,
    CanonicalDocumentContent,
    CanonicalPage,
    ParseError,
    ParseResult,
)

PARSER_NAME = "meyar-local-text-parser"
PARSER_VERSION = "1.0.0"

# Bounds worst-case parse cost independent of the byte-size cap already
# enforced at upload time — a pathological page count is still possible
# within a small file.
_MAX_PAGES = 300


class LocalTextParser:
    """Deterministic local parser for digital (non-scanned) PDF/DOCX, built
    on pypdf and python-docx — no OCR, no ML runtime. See
    docs/DECISIONS.md D-007 for why Docling is deferred on this hardware."""

    async def parse(self, *, data: bytes, document_type: str) -> ParseResult:
        try:
            if document_type == "PDF":
                pages = _parse_pdf(data)
            elif document_type == "DOCX":
                pages = _parse_docx(data)
            else:
                raise ParseError(f"Unsupported document_type: {document_type}")
        except ParseError:
            raise
        except Exception as exc:  # malformed/corrupt document content
            raise ParseError(f"Failed to parse {document_type}: {exc}") from exc

        return ParseResult(
            content=CanonicalDocumentContent(language=None, pages=pages),
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
        )


def _parse_pdf(data: bytes) -> list[CanonicalPage]:
    reader = pypdf.PdfReader(io.BytesIO(data))
    page_count = len(reader.pages)
    if page_count == 0:
        raise ParseError("PDF has no pages.")
    if page_count > _MAX_PAGES:
        raise ParseError(f"PDF exceeds the maximum supported page count ({_MAX_PAGES}).")

    pages: list[CanonicalPage] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        blocks = [CanonicalBlock(index=0, text=text)] if text else []
        pages.append(CanonicalPage(page=page_number, blocks=blocks))
    return pages


def _parse_docx(data: bytes) -> list[CanonicalPage]:
    document = docx.Document(io.BytesIO(data))
    blocks = [
        CanonicalBlock(index=i, text=paragraph.text.strip())
        for i, paragraph in enumerate(document.paragraphs)
        if paragraph.text.strip()
    ]
    if not blocks:
        raise ParseError("DOCX has no extractable text.")
    # DOCX has no fixed pagination at the XML level — represented as one
    # logical page so block references stay stable and addressable.
    return [CanonicalPage(page=1, blocks=blocks)]
