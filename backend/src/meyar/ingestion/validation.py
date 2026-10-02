import io
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_ALLOWED_CONTENT_TYPES = {PDF_MIME, DOCX_MIME, "application/octet-stream", ""}
_EXTENSION_TO_TYPE = {".pdf": "PDF", ".docx": "DOCX"}

SUPPORTED_EXTENSIONS = frozenset(_EXTENSION_TO_TYPE)


class UnsupportedDocumentError(ValueError):
    """Raised for any validation failure: unsupported type, mismatched
    extension/MIME/signature, or malformed content detected pre-storage."""


class DocumentTooLargeError(ValueError):
    pass


def too_large_message(max_bytes: int) -> str:
    """The one outward copy for an over-limit document, shared by the
    application validator and the HTTP request-envelope guard so both
    layers answer identically."""
    return f"File exceeds the maximum allowed size of {max_bytes} bytes."


# --- DOCX archive safety (issue #46 PR-1, docs/DECISIONS.md) -------------
# A DOCX is a ZIP that python-docx loads fully into memory, so the bounds
# below limit what a hostile archive can make this process materialize.
# Real CV DOCX files have tens of members (a few hundred at the very most
# with many embedded images), XML parts of well under a megabyte and
# deflate ratios of roughly 3-20x; every bound leaves an order of
# magnitude of headroom over that and sits far below the 1 GiB+ that a
# 10 MiB deflate bomb can expand to. They are module constants (not
# settings): they are a safety floor, not an operator tuning knob.
DOCX_MAX_MEMBERS = 1000
DOCX_MAX_MEMBER_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
DOCX_MAX_TOTAL_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 100
# Tiny parts (empty rels/props files) legitimately compress >100x; the
# ratio test only applies once a member or the archive expands past this.
DOCX_RATIO_FLOOR_BYTES = 1024 * 1024
# zipfile materializes one ZipInfo per central-directory entry before any
# check could run, so the directory itself is bounded from the
# end-of-central-directory record first (<= 1 KiB per allowed member).
DOCX_MAX_CENTRAL_DIRECTORY_BYTES = DOCX_MAX_MEMBERS * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_MAX_TAIL_BYTES = 22 + 65535
_ZIP_ENCRYPTED_FLAG = 0x1
_SUPPORTED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


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
        raise DocumentTooLargeError(too_large_message(max_bytes))

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
            _verify_docx_archive(data)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
            raise UnsupportedDocumentError("Malformed zip-based document.") from exc
        # DOCX is OOXML-in-zip; a bare zip or another OOXML format (xlsx,
        # pptx) must not be accepted as a document.
        if "word/document.xml" in names:
            return DetectedDocument(document_type="DOCX", mime_type=DOCX_MIME)
        raise UnsupportedDocumentError("Zip-based file is not a supported Office document.")
    raise UnsupportedDocumentError("Unrecognized file signature.")


_UNSAFE_ARCHIVE_MESSAGE = "Zip-based document exceeds safe structural limits."


def _normalized_member_name(name: str) -> str:
    return name.replace("\\", "/").lstrip("./").strip("/").casefold()


def _check_central_directory_bounds(data: bytes) -> None:
    """Bound the central directory from the EOCD record, found exactly the
    way zipfile finds it (last signature in the tail), before zipfile
    builds a ZipInfo per entry."""
    tail = data[-_EOCD_MAX_TAIL_BYTES:]
    position = tail.rfind(_EOCD_SIGNATURE)
    if position < 0 or len(tail) - position < 22:
        raise zipfile.BadZipFile("no end-of-central-directory record")
    fields = struct.unpack("<4sHHHHLLH", tail[position : position + 22])
    entries_total, directory_size = fields[4], fields[5]
    if (
        entries_total > DOCX_MAX_MEMBERS
        or entries_total == 0xFFFF  # zip64 sentinel: never legitimate for a CV
        or directory_size == 0xFFFFFFFF
        or directory_size > DOCX_MAX_CENTRAL_DIRECTORY_BYTES
    ):
        raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _stream_bounded(stream: _Readable, *, member_cap: int, total_remaining: int) -> int:
    """Actually decompress a member in bounded chunks and return the real
    byte count. The count read here, not the ZIP header, is the authority:
    it raises as soon as either cap is crossed."""
    count = 0
    while chunk := stream.read(_STREAM_CHUNK_BYTES):
        count += len(chunk)
        if count > member_cap or count > total_remaining:
            raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
    return count


def _ratio_exceeded(uncompressed: int, compressed: int) -> bool:
    return uncompressed > DOCX_RATIO_FLOOR_BYTES and (
        uncompressed > max(compressed, 1) * DOCX_MAX_COMPRESSION_RATIO
    )


def _verify_docx_archive(data: bytes) -> None:
    """Structural and expansion safety for a zip-based upload, run before
    python-docx or any parser sees it. Nothing is extracted to disk;
    nothing about the archive (member names, paths, sizes) is ever placed
    in an outward error."""
    _check_central_directory_bounds(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > DOCX_MAX_MEMBERS:
            raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
        seen: set[str] = set()
        declared_total = 0
        for info in infos:
            normalized = _normalized_member_name(info.filename)
            if normalized in seen:
                raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
            seen.add(normalized)
            if info.flag_bits & _ZIP_ENCRYPTED_FLAG:
                raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
            if info.is_dir():
                continue
            if info.compress_type not in _SUPPORTED_COMPRESSION:
                raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
            if info.file_size > DOCX_MAX_MEMBER_UNCOMPRESSED_BYTES or _ratio_exceeded(
                info.file_size, info.compress_size
            ):
                raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
            declared_total += info.file_size
        if declared_total > DOCX_MAX_TOTAL_UNCOMPRESSED_BYTES or _ratio_exceeded(
            declared_total, len(data)
        ):
            raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)

        # Headers are attacker-controlled: enforce the same bounds on bytes
        # actually produced by decompression.
        actual_total = 0
        for info in infos:
            if info.is_dir():
                continue
            with archive.open(info) as stream:
                actual = _stream_bounded(
                    stream,
                    member_cap=DOCX_MAX_MEMBER_UNCOMPRESSED_BYTES,
                    total_remaining=DOCX_MAX_TOTAL_UNCOMPRESSED_BYTES - actual_total,
                )
            if _ratio_exceeded(actual, info.compress_size):
                raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
            actual_total += actual
        if _ratio_exceeded(actual_total, len(data)):
            raise UnsupportedDocumentError(_UNSAFE_ARCHIVE_MESSAGE)
