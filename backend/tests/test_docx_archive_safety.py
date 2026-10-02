"""Issue #46 PR-1 — DOCX archive safety. Every archive is generated in-test
from synthetic bytes; no real CV and no large fixture is committed."""

import io
import struct
import warnings
import zipfile
from pathlib import Path

import pytest

from meyar.ingestion import validation
from meyar.ingestion.validation import UnsupportedDocumentError, validate_upload

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"
SECRET_NAME = "secret_member_name_do_not_leak.xml"
MAX = 10 * 1024 * 1024


def _zip(members: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name UserWarning is intended
        with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
            for name, content in members:
                archive.writestr(name, content)
    return buffer.getvalue()


def _validate(data: bytes) -> None:
    validate_upload(filename="cv.docx", content_type="", data=data, max_bytes=MAX)


LIMIT_MESSAGE = "Zip-based document exceeds safe structural limits."


def _rejected(data: bytes, message: str = LIMIT_MESSAGE) -> UnsupportedDocumentError:
    with pytest.raises(UnsupportedDocumentError) as excinfo:
        _validate(data)
    assert str(excinfo.value) == message  # generic: no member names/paths/sizes
    assert SECRET_NAME not in str(excinfo.value)
    return excinfo.value


def test_member_count_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "DOCX_MAX_MEMBERS", 5)
    members = [("word/document.xml", b"<a/>")] + [(f"m{i}.xml", b"<a/>") for i in range(20)]
    members.append((SECRET_NAME, b"<a/>"))
    _rejected(_zip(members))


def test_per_member_expansion_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "DOCX_MAX_MEMBER_UNCOMPRESSED_BYTES", 10_000)
    _rejected(_zip([("word/document.xml", b"0" * 100_000), (SECRET_NAME, b"x")]))


def test_total_expansion_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "DOCX_MAX_TOTAL_UNCOMPRESSED_BYTES", 50_000)
    members = [("word/document.xml", b"0" * 30_000), ("a.xml", b"1" * 30_000)]
    members.append((SECRET_NAME, b"2" * 30_000))
    _rejected(_zip(members))


def test_compression_ratio_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "DOCX_RATIO_FLOOR_BYTES", 1000)
    _rejected(_zip([("word/document.xml", b"0" * 500_000), (SECRET_NAME, b"x")]))


def test_small_highly_compressible_member_is_not_a_ratio_bomb() -> None:
    # Below the ratio floor: an empty-ish part may compress >100x legitimately.
    _validate(_zip([("word/document.xml", b"<w/>" * 50), ("docProps/empty.xml", b" " * 900)]))


def test_encrypted_member_rejected() -> None:
    honest = _zip([("word/document.xml", b"<a/>"), (SECRET_NAME, b"<a/>")])
    position = honest.find(b"PK\x01\x02")  # first central-directory entry
    raw = bytearray(honest)
    raw[position + 8] |= 0x1  # general-purpose bit 0: encrypted
    flagged = bytes(raw)
    assert zipfile.ZipFile(io.BytesIO(flagged)).infolist()[0].flag_bits & 0x1
    _rejected(flagged)


def test_duplicate_normalized_member_name_rejected() -> None:
    _rejected(
        _zip(
            [
                ("word/document.xml", b"<a/>"),
                (f"WORD/{SECRET_NAME}", b"<a/>"),
                (f"word/{SECRET_NAME}", b"<b/>"),
            ]
        )
    )


@pytest.mark.parametrize("payload", [b"PK", b"PK\x03\x04" + b"\x00" * 64, b"PK" + b"junk" * 50])
def test_malformed_zip_rejected(payload: bytes) -> None:
    _rejected(payload, "Malformed zip-based document.")


def _patch_central_directory(data: bytes, *, offset: int, value: int) -> bytes:
    position = data.rfind(b"PK\x01\x02")
    assert position >= 0
    raw = bytearray(data)
    raw[position + offset : position + offset + 4] = struct.pack("<L", value)
    return bytes(raw)


def test_forged_small_size_header_cannot_bypass_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation, "DOCX_MAX_MEMBER_UNCOMPRESSED_BYTES", 10_000)
    honest = _zip([("word/document.xml", b"0" * 100_000)])
    forged = _patch_central_directory(honest, offset=24, value=100)  # file_size lies: 100
    # The header now passes the declared-size check; the real stream is
    # truncated at the forged size and fails integrity (CRC) — never accepted.
    _rejected(forged, "Malformed zip-based document.")


def test_forged_huge_size_header_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    honest = _zip([("word/document.xml", b"<a/>" * 10)])
    forged = _patch_central_directory(honest, offset=24, value=0x7FFFFFFF)
    _rejected(forged)


def test_streaming_count_not_header_is_the_authority() -> None:
    # Directly prove the counter works on produced bytes, independent of
    # any declared size.
    stream = io.BytesIO(b"0" * 5_000)
    with pytest.raises(UnsupportedDocumentError):
        validation._stream_bounded(stream, member_cap=1_000, total_remaining=10_000)
    stream = io.BytesIO(b"0" * 5_000)
    with pytest.raises(UnsupportedDocumentError):
        validation._stream_bounded(stream, member_cap=10_000, total_remaining=1_000)
    assert (
        validation._stream_bounded(io.BytesIO(b"0" * 500), member_cap=1_000, total_remaining=1_000)
        == 500
    )


def test_central_directory_size_bounded_before_zipfile_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(validation, "DOCX_MAX_CENTRAL_DIRECTORY_BYTES", 10)
    _rejected(_zip([("word/document.xml", b"<a/>")]))


def test_normal_synthetic_docx_still_validates() -> None:
    detected = validate_upload(
        filename="valid_cv.docx",
        content_type="",
        data=(FIXTURES_DIR / "valid_cv.docx").read_bytes(),
        max_bytes=MAX,
    )
    assert detected.document_type == "DOCX"


def test_normal_synthetic_pdf_still_validates() -> None:
    detected = validate_upload(
        filename="valid_cv.pdf",
        content_type="application/pdf",
        data=(FIXTURES_DIR / "valid_cv.pdf").read_bytes(),
        max_bytes=MAX,
    )
    assert detected.document_type == "PDF"


def test_non_docx_zip_still_rejected() -> None:
    _rejected(
        _zip([("xl/workbook.xml", b"<a/>")]),
        "Zip-based file is not a supported Office document.",
    )
