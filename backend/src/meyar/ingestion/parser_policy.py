"""Process-local safety floors; rationale and development evidence: D-097."""

from dataclasses import dataclass

PARSER_NAME = "meyar-local-text-parser"
PARSER_VERSION = "1.1.0"
MAX_ACTIVE = 1
MAX_WAITERS = 4
ADMISSION_SECONDS = 3.0
WORKER_SECONDS = 20.0
CLEANUP_SECONDS = 0.5
WORKER_MEMORY_BYTES = 768 * 1024 * 1024


@dataclass(frozen=True)
class OutputLimits:
    pages: int = 300
    blocks: int = 10_000
    characters: int = 1_000_000
    text_bytes: int = 4 * 1024 * 1024
    result_bytes: int = 8 * 1024 * 1024
