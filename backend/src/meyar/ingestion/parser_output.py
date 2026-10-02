"""Canonical acceptance bounds, checked in worker and again in parent."""

from meyar.ingestion.parser import ParseError, ParseFailureCode, ParseResult
from meyar.ingestion.parser_policy import PARSER_NAME, PARSER_VERSION, OutputLimits


class TextBudget:
    def __init__(self, limits: OutputLimits) -> None:
        self.limits = limits
        self.blocks = self.characters = self.text_bytes = 0

    def add(self, text: str) -> None:
        self.blocks += 1
        self.characters += len(text)
        self.text_bytes += len(text.encode("utf-8"))
        if (
            self.blocks > self.limits.blocks
            or self.characters > self.limits.characters
            or self.text_bytes > self.limits.text_bytes
        ):
            raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)


def validate_result(result: ParseResult, kind: str, limits: OutputLimits) -> None:
    if (
        result.parser_name != PARSER_NAME
        or result.parser_version != PARSER_VERSION
        or result.content.language is not None
    ):
        raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT)
    pages = result.content.pages
    if not pages:
        raise ParseError(ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT)
    if len(pages) > limits.pages:
        raise ParseError(ParseFailureCode.PARSER_OUTPUT_LIMIT)
    if kind == "DOCX" and len(pages) != 1:
        raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT)
    budget = TextBudget(limits)
    for number, page in enumerate(pages, 1):
        if page.page != number or (kind == "PDF" and len(page.blocks) > 1):
            raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT)
        previous = -1
        for block in page.blocks:
            # DOCX indexes include empty source paragraphs; preserve those gaps.
            if block.index <= previous or (kind == "PDF" and block.index != 0):
                raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT)
            if not block.text.strip() or block.text != block.text.strip():
                raise ParseError(ParseFailureCode.INVALID_PARSER_OUTPUT)
            previous = block.index
            budget.add(block.text)
    if not budget.blocks:
        raise ParseError(ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT)
