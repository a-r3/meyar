import uuid

from pydantic import BaseModel

from meyar.extraction.redaction import redact_block_text
from meyar.models.canonical_document import CanonicalDocument


class ModelInputBlock(BaseModel):
    page: int
    block_index: int
    text: str


class ProfessionalDocumentView(BaseModel):
    """The bounded, redacted payload actually shown to the LLM. Never the
    raw CanonicalDocument. Source page/block references are preserved so
    extracted evidence can be verified back against this exact view. See
    docs/MASTER_SPEC.md §6/§9 (Slice 4 spec)."""

    canonical_document_id: uuid.UUID
    blocks: list[ModelInputBlock]

    def total_chars(self) -> int:
        return sum(len(block.text) for block in self.blocks)


def build_professional_document_view(canonical: CanonicalDocument) -> ProfessionalDocumentView:
    blocks: list[ModelInputBlock] = []
    for page in canonical.content.get("pages", []):
        for block in page.get("blocks", []):
            blocks.append(
                ModelInputBlock(
                    page=page["page"],
                    block_index=block["index"],
                    text=redact_block_text(block["text"]),
                )
            )
    return ProfessionalDocumentView(canonical_document_id=canonical.id, blocks=blocks)
