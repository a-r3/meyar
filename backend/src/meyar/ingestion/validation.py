import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_ALLOWED_CONTENT_TYPES = {PDF_MIME, DOCX_MIME, "application/octet-stream", ""}
_EXTENSION_TO_TYPE = {".pdf": "PDF", ".docx": "DOCX"}


class UnsupportedDocumentError(ValueError):
    """Raised for any validation failure: unsupported type, mismatched
    extension/MIME/signature, or malformed content detected pre-storage."""


class DocumentTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class DetectedDocument:
    document_type: str  # "PDF" | "DOCX"
    mime_type: str


def validate_upload(
    *, filename: str, content_type: str, data: bytes, max_bytes: int
) -> DetectedDocument:
    """Validate an uploaded candidate document against extension, declared
    content type, and actual file signature — all three must agree. Never
    trusts the filename for anything beyond this check; the filename is
    never used to construct a storage path. See docs/SECURITY_PRIVACY.md."""
    if not data:
        raise UnsupportedDocumentError("Empty file.")
    if len(data) > max_bytes:
        raise DocumentTooLargeError(
            f"File exceeds the maximum allowed size of {max_bytes} bytes."
        )

    detected = _detect_signature(data)

    # PurePosixPath used only to read the suffix — filename is never used
    # to build a real filesystem path anywhere in the ingestion pipeline.
    ext = PurePosixPath(filename or "").suffix.lower()
    expected_type_from_ext = _EXTENSION_TO_TYPE.get(ext)
    if expected_type_from_ext is None:
        raise UnsupportedDocumentError(
            f"Unsupported or missing file extension: '{ext or '(none)'}'."
        )
    if expected_type_from_ext != detected.document_type:
        raise UnsupportedDocumentError(
            "File extension does not match the detected document content."
        )

    normalized_content_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_content_type not in _ALLOWED_CONTENT_TYPES:
        raise UnsupportedDocumentError(
            "Declared content type is not a supported document type."
        )
    if normalized_content_type in (PDF_MIME, DOCX_MIME) and (
        normalized_content_type != detected.mime_type
    ):
        raise UnsupportedDocumentError(
            "Declared content type does not match the detected document content."
        )

    return detected


def _detect_signature(data: bytes) -> DetectedDocument:
    if data[:5] == b"%PDF-":
        return DetectedDocument(document_type="PDF", mime_type=PDF_MIME)
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except zipfile.BadZipFile as exc:
            raise UnsupportedDocumentError("Malformed zip-based document.") from exc
        # DOCX is OOXML-in-zip; a bare zip or another OOXML format (xlsx,
        # pptx) must not be accepted as a document.
        if "word/document.xml" in names:
            return DetectedDocument(document_type="DOCX", mime_type=DOCX_MIME)
        raise UnsupportedDocumentError("Zip-based file is not a supported Office document.")
    raise UnsupportedDocumentError("Unrecognized file signature.")
