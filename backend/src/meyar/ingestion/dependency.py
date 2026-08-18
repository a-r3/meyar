from meyar.ingestion.parser import DocumentParser
from meyar.ingestion.parsers.local_text_parser import LocalTextParser


def get_document_parser() -> DocumentParser:
    return LocalTextParser()
