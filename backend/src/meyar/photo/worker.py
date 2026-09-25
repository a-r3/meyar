"""Untrusted DOCX/PDF raster inspection runs only in this short-lived process.

Input CV bytes arrive on stdin. Output is one bounded JSON result on stdout.
No candidate identity, filename, storage path, or original bytes enter logs.
"""

import base64
import hashlib
import io
import json
import posixpath
import sys
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader

from meyar.photo import policy

_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_R = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class UnsafeImage(Exception):
    pass


@dataclass(frozen=True)
class SourceImage:
    data: bytes
    kind: str
    locator: str
    page: int | None
    scan_like: bool = False


def _bounded_read(archive: ZipFile, name: str, cap: int) -> bytes:
    with archive.open(name) as stream:
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise UnsafeImage("BYTE_LIMIT")
    return data


def _docx_images(data: bytes) -> list[SourceImage]:
    with ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) > policy.MAX_ZIP_ENTRIES:
            raise UnsafeImage("ZIP_ENTRY_LIMIT")
        if len(names) != len(set(names)):
            raise UnsafeImage("ZIP_DUPLICATE_ENTRY")
        if any(name.startswith("/") or "\\" in name or ".." in name.split("/") for name in names):
            raise UnsafeImage("ZIP_UNSAFE_ENTRY")
        if "word/document.xml" not in names or "word/_rels/document.xml.rels" not in names:
            raise UnsafeImage("ZIP_MISSING_BODY")
        for info in infos:
            if (
                info.filename.startswith("word/media/")
                and info.file_size > policy.MAX_SOURCE_IMAGE_BYTES
            ):
                raise UnsafeImage("MEDIA_BYTE_LIMIT")
            if info.filename in ("word/document.xml", "word/_rels/document.xml.rels"):
                if info.file_size > policy.MAX_XML_BYTES:
                    raise UnsafeImage("XML_BYTE_LIMIT")
        body = ET.fromstring(_bounded_read(archive, "word/document.xml", policy.MAX_XML_BYTES))
        rels = ET.fromstring(
            _bounded_read(archive, "word/_rels/document.xml.rels", policy.MAX_XML_BYTES)
        )
        relationships: dict[str, str] = {}
        for rel in rels.findall(f"{_PKG_R}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            if not (rel.get("Type") or "").endswith("/image"):
                continue
            target = rel.get("Target") or ""
            part = posixpath.normpath(posixpath.join("word", target))
            if part.startswith("word/media/") and part in names:
                relationships[rel.get("Id") or ""] = part
        images: list[SourceImage] = []
        total_bytes = 0
        for order, blip in enumerate(body.iter(f"{_A}blip")):
            relationship_id = blip.get(f"{_R}embed")
            if not relationship_id or relationship_id not in relationships:
                continue
            if len(images) >= policy.MAX_IMAGE_OBJECTS:
                raise UnsafeImage("IMAGE_COUNT_LIMIT")
            part = relationships[relationship_id]
            encoded = _bounded_read(archive, part, policy.MAX_SOURCE_IMAGE_BYTES)
            total_bytes += len(encoded)
            if total_bytes > policy.MAX_TOTAL_SOURCE_BYTES:
                raise UnsafeImage("TOTAL_IMAGE_BYTES")
            images.append(
                SourceImage(encoded, "DOCX", f"{part}#body-blip-{order}:{relationship_id}", None)
            )
        return images


def _pdf_images(data: bytes) -> list[SourceImage]:
    reader = PdfReader(io.BytesIO(data), strict=True)
    images: list[SourceImage] = []
    total_bytes = 0
    for page_index, page in enumerate(reader.pages[: policy.PDF_MAX_PAGES]):
        page_width = float(page.mediabox.width)
        page_height = float(page.mediabox.height)
        page_ratio = page_width / page_height if page_height > 0 else 0.0
        resources = page.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            continue
        xobjects = xobjects.get_object()
        if len(xobjects) > policy.MAX_IMAGE_OBJECTS:
            raise UnsafeImage("IMAGE_COUNT_LIMIT")
        for name, reference in xobjects.items():
            obj = reference.get_object()
            if obj.get("/Subtype") != "/Image":
                continue  # nested Form XObjects are outside the MVP extraction scope
            if len(images) >= policy.MAX_IMAGE_OBJECTS:
                raise UnsafeImage("IMAGE_COUNT_LIMIT")
            width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
            if (
                not width
                or not height
                or width > policy.MAX_DIMENSION
                or height > policy.MAX_DIMENSION
            ):
                raise UnsafeImage("DIMENSION_LIMIT")
            if width * height > policy.MAX_PIXELS:
                raise UnsafeImage("PIXEL_LIMIT")
            encoded_size = len(getattr(obj, "_data", b""))
            if encoded_size > policy.MAX_SOURCE_IMAGE_BYTES:
                raise UnsafeImage("MEDIA_BYTE_LIMIT")
            filters = obj.get("/Filter", [])
            if isinstance(filters, str):
                filters = [filters]
            if any(
                str(item) not in ("/DCTDecode", "/FlateDecode", "/ASCII85Decode", "/ASCIIHexDecode")
                for item in filters
            ):
                continue
            # pypdf converts PDF raster streams to JPEG/PNG here. This is
            # intentionally inside the disposable worker, after metadata preflight.
            extracted = page.images[str(name)]
            if len(extracted.data) > policy.MAX_SOURCE_IMAGE_BYTES:
                raise UnsafeImage("MEDIA_BYTE_LIMIT")
            total_bytes += len(extracted.data)
            if total_bytes > policy.MAX_TOTAL_SOURCE_BYTES:
                raise UnsafeImage("TOTAL_IMAGE_BYTES")
            object_id = getattr(reference, "idnum", None)
            locator = f"page-{page_index + 1}/xobject-{name}/object-{object_id or 'direct'}"
            scan_like = width >= 700 and height >= 900 and abs(width / height - page_ratio) < 0.06
            images.append(SourceImage(extracted.data, "PDF", locator, page_index + 1, scan_like))
    return images


def _sanitize(data: bytes) -> tuple[bytes, int, int]:
    if len(data) > policy.MAX_SOURCE_IMAGE_BYTES:
        raise UnsafeImage("MEDIA_BYTE_LIMIT")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image: Image.Image = Image.open(io.BytesIO(data))
        if image.format not in ("JPEG", "PNG") or getattr(image, "n_frames", 1) != 1:
            raise UnsafeImage("UNSUPPORTED_RASTER")
        width, height = image.size
        if (
            width > policy.MAX_DIMENSION
            or height > policy.MAX_DIMENSION
            or width * height > policy.MAX_PIXELS
        ):
            raise UnsafeImage("PIXEL_LIMIT")
        image.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            image = rgb
        else:
            image = image.convert("RGB")
        image.thumbnail(
            (policy.DERIVED_MAX_EDGE, policy.DERIVED_MAX_EDGE), Image.Resampling.LANCZOS
        )
        output = io.BytesIO()
        image.save(
            output, format="JPEG", quality=82, optimize=False, progressive=False, subsampling=0
        )
        result = output.getvalue()
        if len(result) > policy.MAX_DERIVED_BYTES:
            raise UnsafeImage("DERIVED_BYTE_LIMIT")
        return result, image.width, image.height


def extract(data: bytes, kind: str) -> dict:
    if len(data) > policy.MAX_CV_BYTES:
        return {"status": "UNUSABLE", "reason_code": "CV_BYTE_LIMIT"}
    try:
        sources = _docx_images(data) if kind == "DOCX" else _pdf_images(data)
        if not sources:
            return {"status": "NO_PHOTO", "reason_code": "NO_EMBEDDED_RASTER"}
        plausible: list[tuple[SourceImage, bytes, int, int]] = []
        seen_hashes: set[str] = set()
        invalid = 0
        scanned = 0
        for source in sources[: policy.MAX_DECODE_CANDIDATES]:
            if source.scan_like:
                scanned += 1
                continue  # a full-page scan is not an automatically cropped portrait
            source_hash = hashlib.sha256(source.data).hexdigest()
            if source_hash in seen_hashes:
                continue
            seen_hashes.add(source_hash)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(source.data)) as probe:
                        width, height = probe.size
                        if (
                            width < policy.MIN_PORTRAIT_WIDTH
                            or height < policy.MIN_PORTRAIT_HEIGHT
                            or not policy.MIN_PORTRAIT_RATIO
                            <= width / height
                            <= policy.MAX_PORTRAIT_RATIO
                            or width * height > policy.MAX_PORTRAIT_AREA
                        ):
                            continue
                jpeg, actual_width, actual_height = _sanitize(source.data)
            except (
                UnsafeImage,
                UnidentifiedImageError,
                OSError,
                ValueError,
                Image.DecompressionBombError,
            ):
                invalid += 1
                continue
            plausible.append((source, jpeg, actual_width, actual_height))
            if len(plausible) > 1:
                return {"status": "AMBIGUOUS", "reason_code": "MULTIPLE_PLAUSIBLE_IMAGES"}
        if len(sources) > policy.MAX_DECODE_CANDIDATES:
            return {"status": "UNUSABLE", "reason_code": "DECODE_CANDIDATE_LIMIT"}
        if not plausible:
            return {
                "status": "UNUSABLE" if invalid or scanned else "NO_PHOTO",
                "reason_code": "NO_PLAUSIBLE_IMAGE",
            }
        source, jpeg, width, height = plausible[0]
        return {
            "status": "AVAILABLE",
            "reason_code": None,
            "source_kind": source.kind,
            "source_locator": source.locator,
            "source_page": source.page,
            "source_image_sha256": hashlib.sha256(source.data).hexdigest(),
            "derived_sha256": hashlib.sha256(jpeg).hexdigest(),
            "mime_type": "image/jpeg",
            "width": width,
            "height": height,
            "jpeg_base64": base64.b64encode(jpeg).decode("ascii"),
        }
    except (UnsafeImage, BadZipFile, ET.ParseError, OSError, ValueError, KeyError, TypeError):
        return {"status": "UNUSABLE", "reason_code": "UNSAFE_OR_UNSUPPORTED_SOURCE"}
    except Exception:
        return {"status": "EXTRACTION_FAILED", "reason_code": "WORKER_EXTRACTION_FAILED"}


def main() -> None:
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS, (policy.WORKER_MEMORY_BYTES, policy.WORKER_MEMORY_BYTES)
        )
    kind = sys.argv[1]
    result = extract(sys.stdin.buffer.read(policy.MAX_CV_BYTES + 1), kind)
    sys.stdout.write(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
