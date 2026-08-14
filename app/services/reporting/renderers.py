"""Turning a table of rows into a file somebody can open.

CSV, JSON, and HTML need nothing beyond the standard library. XLSX needs
``openpyxl`` and says so plainly when it is absent, rather than producing a CSV
with the wrong extension - a file that lies about its format is worse than one
that is missing.

PDF is written directly. A report is a table of text, which is the one case
where hand-writing PDF is reasonable: fixed Helvetica, a page of rows, no
images, no embedded fonts. The alternative was a headless-browser dependency in
every container for the sake of a rent roll. The costs are real and bounded:
text is encoded as WinAnsi, so characters outside Latin-1 are replaced rather
than rendered, and columns are laid out on a fixed grid rather than measured.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import Any

from app.errors import ValidationFailed
from app.models.reporting import ReportFormat

__all__ = [
    "RenderedReport",
    "render",
    "supported_formats",
]

#: Page geometry, in PDF points (72 to the inch). US Letter, landscape - a rent
#: roll has more columns than a portrait page can hold.
PAGE_WIDTH = 792
PAGE_HEIGHT = 612
MARGIN = 36
LINE_HEIGHT = 14
HEADER_HEIGHT = 48
FONT_SIZE = 9
TITLE_SIZE = 14


@dataclass(frozen=True)
class RenderedReport:
    content: bytes
    content_type: str
    extension: str

    @property
    def size(self) -> int:
        return len(self.content)


def supported_formats() -> list[ReportFormat]:
    formats = [ReportFormat.CSV, ReportFormat.JSON, ReportFormat.HTML, ReportFormat.PDF]
    try:  # pragma: no cover - depends on the deployment's extras
        import openpyxl  # noqa: F401

        formats.append(ReportFormat.XLSX)
    except ImportError:
        pass
    return formats


def render(
    *,
    fmt: ReportFormat,
    title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    generated_at: str | None = None,
) -> RenderedReport:
    """Render rows in the requested format."""
    if fmt == ReportFormat.CSV:
        return _csv(columns, rows)
    if fmt == ReportFormat.JSON:
        return _json(title, columns, rows, generated_at)
    if fmt == ReportFormat.HTML:
        return _html(title, columns, rows, generated_at)
    if fmt == ReportFormat.PDF:
        return _pdf(title, columns, rows, generated_at)
    if fmt == ReportFormat.XLSX:
        return _xlsx(title, columns, rows)
    raise ValidationFailed(f"Unsupported report format {fmt!r}.")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


# ---------------------------------------------------------------------------
# Text formats
# ---------------------------------------------------------------------------


def _csv(columns: list[str], rows: list[dict[str, Any]]) -> RenderedReport:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _cell(row.get(column)) for column in columns})
    # BOM so Excel opens UTF-8 correctly rather than mangling every accent.
    return RenderedReport(
        content=b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8"),
        content_type="text/csv; charset=utf-8",
        extension="csv",
    )


def _json(
    title: str, columns: list[str], rows: list[dict[str, Any]], generated_at: str | None
) -> RenderedReport:
    body = {
        "title": title,
        "generated_at": generated_at,
        "columns": columns,
        "row_count": len(rows),
        "rows": [{column: row.get(column) for column in columns} for row in rows],
    }
    return RenderedReport(
        content=json.dumps(body, indent=2, default=str).encode("utf-8"),
        content_type="application/json",
        extension="json",
    )


def _html(
    title: str, columns: list[str], rows: list[dict[str, Any]], generated_at: str | None
) -> RenderedReport:
    head = "".join(f"<th>{escape(column)}</th>" for column in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(_cell(row.get(c)))}</td>" for c in columns) + "</tr>"
        for row in rows
    )
    document = (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:2rem;color:#12151a}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border-bottom:1px solid #dfe3e8;padding:.5rem .6rem;text-align:left}"
        "th{background:#f5f7fa;font-weight:600}"
        "caption{text-align:left;font-size:1.3rem;font-weight:600;margin-bottom:.6rem}"
        "small{color:#5b6472}</style>"
        f"<table><caption>{escape(title)}"
        f"<br><small>{escape(generated_at or '')} &middot; {len(rows)} rows</small></caption>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )
    return RenderedReport(
        content=document.encode("utf-8"),
        content_type="text/html; charset=utf-8",
        extension="html",
    )


def _xlsx(title: str, columns: list[str], rows: list[dict[str, Any]]) -> RenderedReport:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise ValidationFailed(
            "XLSX output needs the 'openpyxl' package. Install Atlas with the "
            "'reports' extra, or choose CSV."
        ) from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31] or "Report"
    sheet.append(columns)
    for row in rows:
        sheet.append([row.get(column) for column in columns])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return RenderedReport(
        content=buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extension="xlsx",
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _pdf_text(value: str) -> str:
    """Escape a string for a PDF literal and fold it into WinAnsi.

    An unbalanced parenthesis in a property name would otherwise corrupt the
    whole file, so the three structural characters are escaped first and
    anything outside Latin-1 is replaced rather than emitted raw.
    """
    folded = value.encode("latin-1", "replace").decode("latin-1")
    return folded.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf(
    title: str, columns: list[str], rows: list[dict[str, Any]], generated_at: str | None
) -> RenderedReport:
    usable = PAGE_WIDTH - 2 * MARGIN
    column_width = usable / max(len(columns), 1)
    # Characters that fit in a column at this font size, on Helvetica's average
    # advance of roughly half the point size.
    max_chars = max(4, int(column_width / (FONT_SIZE * 0.5)) - 1)
    rows_per_page = max(1, (PAGE_HEIGHT - MARGIN - HEADER_HEIGHT - MARGIN) // LINE_HEIGHT - 1)

    pages: list[list[dict[str, Any]]] = [
        rows[start : start + rows_per_page] for start in range(0, len(rows), rows_per_page)
    ] or [[]]

    streams = [
        _pdf_page_stream(
            title=title,
            columns=columns,
            rows=page,
            generated_at=generated_at,
            page_number=number,
            page_count=len(pages),
            column_width=column_width,
            max_chars=max_chars,
        )
        for number, page in enumerate(pages, start=1)
    ]
    return RenderedReport(
        content=_pdf_document(streams),
        content_type="application/pdf",
        extension="pdf",
    )


def _pdf_page_stream(
    *,
    title: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    generated_at: str | None,
    page_number: int,
    page_count: int,
    column_width: float,
    max_chars: int,
) -> bytes:
    parts: list[str] = ["BT", f"/F2 {TITLE_SIZE} Tf", f"1 0 0 1 {MARGIN} {PAGE_HEIGHT - MARGIN} Tm"]
    parts.append(f"({_pdf_text(title)}) Tj")

    subtitle = f"{generated_at or ''}   page {page_number} of {page_count}"
    parts += [
        f"/F1 {FONT_SIZE} Tf",
        f"1 0 0 1 {MARGIN} {PAGE_HEIGHT - MARGIN - 16} Tm",
        f"({_pdf_text(subtitle.strip())}) Tj",
    ]

    top = PAGE_HEIGHT - MARGIN - HEADER_HEIGHT
    parts.append(f"/F2 {FONT_SIZE} Tf")
    for index, column in enumerate(columns):
        x = MARGIN + index * column_width
        parts += [f"1 0 0 1 {x:.1f} {top} Tm", f"({_pdf_text(column[:max_chars])}) Tj"]

    parts.append(f"/F1 {FONT_SIZE} Tf")
    for line, row in enumerate(rows, start=1):
        y = top - line * LINE_HEIGHT
        for index, column in enumerate(columns):
            x = MARGIN + index * column_width
            text = _cell(row.get(column))[:max_chars]
            parts += [f"1 0 0 1 {x:.1f} {y:.1f} Tm", f"({_pdf_text(text)}) Tj"]

    parts.append("ET")
    return "\n".join(parts).encode("latin-1", "replace")


def _pdf_document(streams: list[bytes]) -> bytes:
    """Assemble a minimal but valid PDF 1.4 file with a correct xref table."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)  # object numbers are 1-based

    # 1: catalog, 2: pages - reserved so children can point at them.
    catalog_number = add(b"")
    pages_number = add(b"")

    font_regular = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    font_bold = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )

    page_numbers: list[int] = []
    for stream in streams:
        content_number = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_numbers.append(
            add(
                b"<< /Type /Page /Parent "
                + str(pages_number).encode()
                + b" 0 R /MediaBox [0 0 "
                + str(PAGE_WIDTH).encode()
                + b" "
                + str(PAGE_HEIGHT).encode()
                + b"] /Resources << /Font << /F1 "
                + str(font_regular).encode()
                + b" 0 R /F2 "
                + str(font_bold).encode()
                + b" 0 R >> >> /Contents "
                + str(content_number).encode()
                + b" 0 R >>"
            )
        )

    kids = b" ".join(str(number).encode() + b" 0 R" for number in page_numbers)
    objects[pages_number - 1] = (
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_numbers)).encode() + b" >>"
    )
    objects[catalog_number - 1] = (
        b"<< /Type /Catalog /Pages " + str(pages_number).encode() + b" 0 R >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_number).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)
