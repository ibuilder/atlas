"""Document services.

SPDX-License-Identifier: MIT
"""

from app.services.documents.service import (
    documents_for,
    link_document,
    open_document,
    purge_expired_documents,
    record_scan_result,
    resolve_signed_token,
    sign_document_token,
    unlink_document,
    upload_document,
)
from app.services.documents.storage import ALLOWED_EXTENSIONS, get_storage

__all__ = [
    "ALLOWED_EXTENSIONS",
    "documents_for",
    "get_storage",
    "link_document",
    "open_document",
    "purge_expired_documents",
    "record_scan_result",
    "resolve_signed_token",
    "sign_document_token",
    "unlink_document",
    "upload_document",
]
