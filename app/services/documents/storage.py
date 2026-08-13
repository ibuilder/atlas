"""Object storage behind one interface.

Two rules shape everything here.

**The storage key is never derived from the user's filename.** A key built from
untrusted input is a path traversal waiting to happen, and it leaks resident
names into object listings and CDN logs. Keys are
``<tenant prefix>/<year>/<month>/<uuid><ext>``; the original filename lives in a
database column where it belongs.

**Bytes are trusted over declarations.** The declared content type is a hint from
the client. The first few bytes of the file are the fact. A ``.pdf`` whose bytes
say ``<?php`` is rejected at the boundary, not stored and puzzled over later.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Protocol

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.types import uuid7_str

__all__ = [
    "ALLOWED_EXTENSIONS",
    "LocalStorage",
    "S3Storage",
    "StorageAdapter",
    "StoredObject",
    "build_storage_key",
    "get_storage",
    "sniff_content_type",
]

log = get_logger("services.documents.storage")

QUARANTINE_PREFIX = "quarantine"

#: Extensions Atlas will accept. An allowlist rather than a denylist: the set of
#: dangerous extensions is open-ended and grows with every new interpreter, while
#: the set of documents a property manager needs is small and stable.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".heic",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".csv",
        ".txt",
        ".rtf",
        ".md",
        ".zip",
        ".mp4",
        ".mov",
        ".m4v",
    }
)

#: Magic-byte signatures. Deliberately a small hand-rolled table rather than a
#: libmagic dependency: the set of formats Atlas accepts is closed, and a native
#: library in the upload path is a large attack surface for a small convenience.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),  # legacy Office
    (b"{\\rtf", "application/rtf"),
)

#: Byte sequences that must never appear at the head of an accepted upload,
#: whatever the extension says. These are the shapes that turn a document store
#: into a code-execution path when something downstream is careless.
_FORBIDDEN_HEADS: tuple[bytes, ...] = (
    b"<?php",
    b"#!/",
    b"\x7fELF",  # Linux executable
    b"\xca\xfe\xba\xbe",  # Mach-O / Java class
    b"<script",
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]")
_MAX_SNIFF = 512


@dataclass(frozen=True)
class StoredObject:
    """The result of persisting bytes."""

    key: str
    size_bytes: int
    checksum_sha256: str
    content_type: str
    quarantined: bool


class StorageAdapter(Protocol):
    """The contract every backend implements."""

    def put(self, key: str, stream: IO[bytes]) -> int: ...
    def get(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def move(self, source_key: str, destination_key: str) -> None: ...


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def sniff_content_type(head: bytes, declared: str | None, filename: str) -> str:
    """Determine the real content type, and refuse obvious weapons.

    Returns the resolved type. Raises when the bytes are forbidden outright, or
    when a signature is recognised and contradicts the declared type.
    """
    if _looks_like_dos_executable(head):
        log.warning(
            "upload rejected on magic bytes",
            extra={"event": "security.upload_rejected", "reason": "dos_header"},
        )
        raise ValidationFailed(
            "That file type cannot be uploaded.",
            details=[
                {"field": "file", "message": "The file content is not a permitted document type."}
            ],
        )

    for forbidden in _FORBIDDEN_HEADS:
        if head.startswith(forbidden):
            log.warning(
                "upload rejected on magic bytes",
                extra={"event": "security.upload_rejected", "reason": "forbidden_signature"},
            )
            raise ValidationFailed(
                "That file type cannot be uploaded.",
                details=[
                    {
                        "field": "file",
                        "message": "The file content is not a permitted document type.",
                    }
                ],
            )

    detected: str | None = None
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            detected = content_type
            break

    # Zip-based Office formats and plain zips share a signature, so the
    # extension disambiguates what the bytes cannot.
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        detected = _zip_family(filename)

    if detected is None:
        # No signature: plain text, CSV, and Markdown have none. Accept the
        # declared type only if the extension is on the allowlist, which it has
        # already been checked against.
        return (declared or "application/octet-stream").split(";")[0].strip()

    if detected == "application/x-ole-storage":
        return _ole_family(filename)

    if declared:
        declared_type = declared.split(";")[0].strip().lower()
        if declared_type and _family(declared_type) != _family(detected):
            log.warning(
                "upload rejected: declared type contradicts content",
                extra={
                    "event": "security.upload_type_mismatch",
                    "declared": declared_type,
                    "detected": detected,
                },
            )
            raise ValidationFailed(
                "The file content does not match its declared type.",
                details=[
                    {
                        "field": "file",
                        "message": f"Declared {declared_type}, but the content is {detected}.",
                    }
                ],
            )
    return detected


def _looks_like_dos_executable(head: bytes) -> bool:
    """Whether the head is a DOS/PE header rather than text that starts "MZ".

    Matching on the two bytes alone is too coarse: a CSV whose first cell is a
    unit code like ``MZ-1``, or a resident named Mzamo, begins with exactly
    those bytes and would be rejected with no way to tell why. A real DOS header
    is followed by a byte count field, so byte three is not printable text, and
    a PE additionally carries the ``PE\\0\\0`` marker further in.
    """
    if not head.startswith(b"MZ") or len(head) < 3:
        return False
    if b"PE\x00\x00" in head[:512] or b"This program cannot be run" in head[:256]:
        return True
    # Byte 2 of a DOS header is the low byte of "bytes on last page"; in text it
    # would be an ordinary printable character.
    return head[2] not in range(0x20, 0x7F)


def _family(content_type: str) -> str:
    """Compare by family, so image/jpg and image/jpeg do not fight."""
    top, _, sub = content_type.partition("/")
    if top == "image":
        return "image/" + {"jpg": "jpeg", "tif": "tiff"}.get(sub, sub)
    return content_type


def _zip_family(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }.get(suffix, "application/zip")


def _ole_family(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".doc": "application/msword",
        ".xls": "application/vnd.ms-excel",
        ".ppt": "application/vnd.ms-powerpoint",
    }.get(suffix, "application/x-ole-storage")


def validate_filename(filename: str) -> tuple[str, str]:
    """Return a safe display name and its extension, or raise."""
    if not filename or not filename.strip():
        raise ValidationFailed("A filename is required.")

    # Take the basename only: a client may legitimately send a path, and the
    # directory portion is never something we want.
    base = os.path.basename(filename.replace("\\", "/")).strip()
    suffix = Path(base).suffix.lower()

    if suffix not in ALLOWED_EXTENSIONS:
        raise ValidationFailed(
            f"Files of type {suffix or 'unknown'} cannot be uploaded.",
            details=[
                {
                    "field": "file",
                    "message": "Permitted types: " + ", ".join(sorted(ALLOWED_EXTENSIONS)),
                }
            ],
        )

    safe = _SAFE_NAME.sub("_", base)[:255]
    return safe, suffix


def build_storage_key(*, tenant_prefix: str, extension: str, at: dt.date | None = None) -> str:
    """Generate an opaque, tenant-scoped key.

    Date-partitioned so a bucket listing stays navigable at scale, and prefixed
    per tenant so one organization's keys can never address another's.

    The key is **stable for the document's lifetime**. An earlier design wrote
    quarantined uploads under a separate prefix and renamed them on a clean
    scan, for physical separation. That rename cannot be made atomic with the
    database update: if the object moves and the transaction then rolls back,
    the row points at a key that no longer exists and the document is
    permanently unreadable. Quarantine is enforced by ``Document.is_servable``,
    which is checked on every retrieval path and cannot get out of step with
    itself.

    UTC, not local time: every other timestamp in the system is UTC, and a
    worker in another timezone would otherwise file an upload received at 23:30
    into the wrong month.
    """
    when = at or dt.datetime.now(dt.UTC).date()
    prefix = tenant_prefix.strip("/") or "org/unknown"
    return f"{prefix}/{when:%Y/%m}/{uuid7_str()}{extension}"


def digest_and_size(stream: IO[bytes], *, max_bytes: int) -> tuple[str, int, bytes]:
    """Stream the payload once, returning its digest, size, and leading bytes.

    Streamed rather than read whole: a 50MB upload held entirely in memory per
    concurrent request is how an upload endpoint becomes a denial-of-service.
    """
    hasher = hashlib.sha256()
    size = 0
    head = b""

    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        if not head:
            head = chunk[:_MAX_SNIFF]
        size += len(chunk)
        if size > max_bytes:
            raise ValidationFailed(
                f"The file exceeds the {max_bytes // (1024 * 1024)}MB limit.",
                code="payload_too_large",
            )
        hasher.update(chunk)

    if size == 0:
        raise ValidationFailed("The uploaded file is empty.")

    stream.seek(0)
    return hasher.hexdigest(), size, head


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class LocalStorage:
    """Filesystem backend for development and single-node deployments.

    Production refuses to start with this configured: local disk is not durable
    across replicas, and a document written on one pod is invisible to the next.
    """

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        # Belt and braces. Keys are generated, never user-supplied, but a
        # traversal check on the resolved path costs nothing and this is the
        # one place where being wrong is unrecoverable.
        if not candidate.is_relative_to(self.root):
            raise ValidationFailed("Invalid storage key.")
        return candidate

    def put(self, key: str, stream: IO[bytes]) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            shutil.copyfileobj(stream, handle, length=64 * 1024)
        return path.stat().st_size

    def get(self, key: str) -> BinaryIO:
        path = self._path(key)
        if not path.exists():
            from app.errors import NotFound

            raise NotFound("The stored object was not found.")
        return path.open("rb")

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def move(self, source_key: str, destination_key: str) -> None:
        source = self._path(source_key)
        destination = self._path(destination_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)


class S3Storage:
    """S3-compatible backend. Also covers MinIO, R2, and Spaces."""

    def __init__(self, bucket: str, region: str = "", endpoint_url: str = "") -> None:
        self.bucket = bucket
        self._client = None
        self._region = region
        self._endpoint_url = endpoint_url

    @property
    def client(self):  # noqa: ANN201
        if self._client is None:
            import boto3  # imported lazily: not a dependency for local development

            self._client = boto3.client(
                "s3",
                region_name=self._region or None,
                endpoint_url=self._endpoint_url or None,
            )
        return self._client

    def put(self, key: str, stream: IO[bytes]) -> int:
        self.client.upload_fileobj(stream, self.bucket, key)
        return self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"]

    def get(self, key: str) -> BinaryIO:
        buffer = io.BytesIO()
        self.client.download_fileobj(self.bucket, key, buffer)
        buffer.seek(0)
        return buffer

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return False
        return True

    def move(self, source_key: str, destination_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket,
            CopySource={"Bucket": self.bucket, "Key": source_key},
            Key=destination_key,
        )
        self.delete(source_key)


def get_storage() -> StorageAdapter:
    """Resolve the configured backend, memoised per application."""
    from flask import current_app

    adapter = current_app.extensions.get("atlas_storage")
    if adapter is not None:
        return adapter

    settings = current_app.config["SETTINGS"]
    if settings.storage_backend == "s3":
        adapter = S3Storage(
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            endpoint_url=settings.storage_endpoint_url,
        )
    else:
        adapter = LocalStorage(settings.storage_local_path)

    current_app.extensions["atlas_storage"] = adapter
    return adapter
