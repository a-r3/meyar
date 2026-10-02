from meyar.ingestion.parser import ParseResult
from meyar.ingestion.parser_supervisor import parse_isolated


class LocalTextParser:
    """Async local digital-PDF/body-paragraph-DOCX parser; no OCR or AI.

    Untrusted library work runs in a bounded, disposable process. Normal
    successful page/block semantics remain those of parser version 1.0.0.
    """

    async def parse(self, *, data: bytes, document_type: str) -> ParseResult:
        return await parse_isolated(data=data, document_type=document_type)
