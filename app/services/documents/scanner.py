"""Malware scanning behind an adapter.

Atlas does not implement virus detection. It implements the *pipeline*: quarantine
on arrival, scan, and release or destroy — so that plugging in ClamAV, an endpoint
API, or a cloud scanner is a configuration change rather than a redesign.

The default adapter is deliberately conservative. It performs structural checks
it can actually do correctly and is explicit that it is not a virus scanner, so
nobody mistakes a green result for one.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from app.logging import get_logger

__all__ = ["ClamAVScanner", "ScanResult", "Scanner", "StructuralScanner", "get_scanner"]

log = get_logger("services.documents.scanner")

#: Patterns that indicate active content inside an otherwise ordinary document.
#: A PDF carrying /JavaScript or /Launch is not automatically malicious, but it
#: is not something a lease packet needs either.
_ACTIVE_CONTENT = (
    re.compile(rb"/JavaScript\b"),
    re.compile(rb"/JS\b"),
    re.compile(rb"/Launch\b"),
    re.compile(rb"/EmbeddedFile\b"),
    re.compile(rb"<script[\s>]", re.IGNORECASE),
    re.compile(rb"vbaProject\.bin"),  # Office macros
)

#: The EICAR test string, so a deployment can prove the pipeline works without
#: handling real malware.
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    detail: str | None = None
    scanner: str = "structural"


class Scanner(Protocol):
    def scan(self, stream: BinaryIO) -> ScanResult: ...


class StructuralScanner:
    """The default. Structural checks, not virus detection.

    Catches the EICAR test file and flags active content. It will not catch a
    real, novel threat, and it does not claim to — ``SECURITY.md`` states this
    plainly so that nobody deploys believing otherwise.
    """

    name = "structural"

    def scan(self, stream: BinaryIO) -> ScanResult:
        payload = stream.read(2 * 1024 * 1024)  # first 2MB is where signatures live
        stream.seek(0)

        if _EICAR in payload:
            return ScanResult(False, "EICAR test signature detected", self.name)

        for pattern in _ACTIVE_CONTENT:
            if pattern.search(payload):
                return ScanResult(
                    False, f"active content detected ({pattern.pattern.decode()})", self.name
                )

        return ScanResult(True, None, self.name)


class ClamAVScanner:
    """Delegates to a clamd daemon over its INSTREAM protocol."""

    name = "clamav"

    def __init__(self, host: str = "127.0.0.1", port: int = 3310, timeout: int = 30) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def scan(self, stream: BinaryIO) -> ScanResult:
        import socket
        import struct

        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(b"zINSTREAM\0")
                while chunk := stream.read(64 * 1024):
                    sock.sendall(struct.pack("!L", len(chunk)) + chunk)
                sock.sendall(struct.pack("!L", 0))
                response = sock.recv(4096).decode("utf-8", "replace").strip()
        except OSError as exc:
            # Scanner unreachable: the document stays quarantined. Failing open
            # here would release unscanned files every time clamd restarts.
            log.error(
                "malware scanner unreachable",
                extra={"event": "document.scanner_unreachable", "detail": str(exc)[:200]},
            )
            return ScanResult(False, "scanner unavailable", self.name)
        finally:
            stream.seek(0)

        if response.endswith("OK"):
            return ScanResult(True, None, self.name)
        return ScanResult(False, response[:200], self.name)


def get_scanner() -> Scanner:
    """Resolve the configured scanner, memoised per application."""
    from flask import current_app

    scanner = current_app.extensions.get("atlas_scanner")
    if scanner is not None:
        return scanner

    settings = current_app.config["SETTINGS"]
    backend = getattr(settings, "malware_scanner", "structural")
    scanner = ClamAVScanner() if backend == "clamav" else StructuralScanner()
    current_app.extensions["atlas_scanner"] = scanner
    return scanner
