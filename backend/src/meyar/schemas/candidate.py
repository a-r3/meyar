import uuid
from datetime import datetime

from pydantic import BaseModel


class CandidateOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime


class CanonicalBlockOut(BaseModel):
    index: int
    text: str


class CanonicalPageOut(BaseModel):
    page: int
    blocks: list[CanonicalBlockOut]


class CanonicalDocumentOut(BaseModel):
    id: uuid.UUID
    parser_name: str
    parser_version: str
    language: str | None
    pages: list[CanonicalPageOut]
    created_at: datetime


class CandidateDocumentOut(BaseModel):
    id: uuid.UUID
    candidate_id: uuid.UUID
    original_filename: str
    mime_type: str
    byte_size: int
    sha256_hash: str
    document_status: str
    parser_status: str
    parser_name: str | None
    parser_version: str | None
    parse_error_code: str | None
    parse_error_message: str | None
    created_at: datetime
    parsed_at: datetime | None
    canonical: CanonicalDocumentOut | None = None
