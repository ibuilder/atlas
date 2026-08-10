"""Document intelligence: extraction as a suggestion, never as a fact.

ADR-0006 says AI arrives behind governance controls, and this is the shape that
requires. Nothing here writes to a lease, an invoice, or the ledger. Extraction
produces *candidate field values*, each with the text it came from and a
confidence, and a person accepts or rejects them. The accept is the write.

That is not caution for its own sake. An extracted rent that silently becomes
the lease's rent is a system that bills the wrong amount and can show no
evidence of how it decided. A suggestion a human accepted is a system where the
answer to "why does it say £3,100?" is a name, a timestamp, and the sentence it
was read from.

Three consequences follow:

* **A suggestion carries its evidence.** The extracted span goes with the
  value, so the reviewer checks the document rather than trusting the number.
* **Accepting is attributed and audited.** The moment a suggestion becomes a
  fact is a decision somebody made.
* **Low confidence is surfaced, not hidden.** Values below the review threshold
  are flagged for attention rather than quietly dropped, because a missing
  field is as wrong as a bad one and much easier to miss.

The extractors here are deterministic pattern matchers over text. They are
genuinely useful on the documents this industry actually handles - which are
templated - and they are honest about what they are. A model-backed extractor
would slot in behind the same interface, and would be subject to the same rule:
its output is a suggestion.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.documents import Document
from app.models.types import quantize_money, utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "EXTRACTORS",
    "REVIEW_THRESHOLD",
    "Extraction",
    "Suggestion",
    "accept_suggestion",
    "extract",
    "known_extractors",
    "reject_suggestion",
]

log = get_logger("services.documents.extraction")

#: Below this, a value is flagged for attention rather than presented as likely.
#: Deliberately high: the cost of a wrong lease rent is far above the cost of a
#: person reading one more line.
REVIEW_THRESHOLD = 0.75

#: Documents longer than this are truncated for extraction. A lease is twenty
#: pages; anything past this is a scan artefact or an attack.
MAX_TEXT_CHARS = 400_000


@dataclass
class Suggestion:
    """One proposed field value, with the evidence for it."""

    field: str
    value: Any
    confidence: float
    #: The text this was read from, so the reviewer checks rather than trusts.
    evidence: str
    #: Character offset in the source text, for highlighting.
    offset: int | None = None
    accepted_at: dt.datetime | None = None
    accepted_by_id: str | None = None
    rejected_at: dt.datetime | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.rejected_at is None


@dataclass
class Extraction:
    """What a document appears to say. None of it is true yet."""

    document_id: str | None
    kind: str
    suggestions: list[Suggestion] = field(default_factory=list)
    #: Fields the extractor looked for and did not find. A missing field is as
    #: wrong as a bad one and much easier to overlook.
    missing: list[str] = field(default_factory=list)

    def by_field(self, name: str) -> Suggestion | None:
        return next((s for s in self.suggestions if s.field == name), None)

    @property
    def needs_review(self) -> list[Suggestion]:
        return [s for s in self.suggestions if s.needs_review]

    @property
    def is_confident(self) -> bool:
        return bool(self.suggestions) and not self.needs_review and not self.missing


# ---------------------------------------------------------------------------
# Reading values out of text
# ---------------------------------------------------------------------------

_MONEY = r"[$£€]?\s?([0-9][0-9,]*(?:\.[0-9]{2})?)"
_DATE_PATTERNS = (
    (r"(\d{4})-(\d{2})-(\d{2})", "%Y-%m-%d"),
    (r"(\d{1,2})/(\d{1,2})/(\d{4})", "%m/%d/%Y"),
    (r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", "%d %B %Y"),
    (r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", "%B %d, %Y"),
)

#: A slash date whose first two components are both twelve or under genuinely
#: cannot be resolved: 12/04/2026 is 12 April in most of the world and 4
#: December in the United States. Guessing silently books a payment a month
#: out, so the reading is kept but its confidence is cut to force a review.
_AMBIGUOUS_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/\d{4}$")


def _is_ambiguous(literal: str) -> bool:
    match = _AMBIGUOUS_SLASH.match(literal.strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first <= 12 and second <= 12 and first != second


def _money_near(text: str, labels: tuple[str, ...]) -> tuple[Decimal, str, int] | None:
    """Find an amount following one of these labels."""
    for label in labels:
        pattern = re.compile(re.escape(label) + r"[^0-9$£€\n]{0,40}" + _MONEY, re.IGNORECASE)
        match = pattern.search(text)
        if match:
            try:
                return (
                    Decimal(match.group(1).replace(",", "")),
                    match.group(0).strip(),
                    match.start(),
                )
            except InvalidOperation:  # pragma: no cover - the regex shape prevents this
                continue
    return None


def _date_near(text: str, labels: tuple[str, ...]) -> tuple[dt.date, str, int, bool] | None:
    """The date *nearest* the label, not the first pattern that happens to match.

    Trying patterns in order would let an ISO date further down the window beat
    a slash-formatted one right beside the label - which on a typical invoice
    means reading the due date as the invoice date. Proximity is the signal;
    the format is not.
    """
    for label in labels:
        window = re.compile(re.escape(label) + r"(.{0,60})", re.IGNORECASE | re.DOTALL)
        found = window.search(text)
        if not found:
            continue

        fragment = found.group(1)
        best: tuple[int, dt.date, bool] | None = None
        for pattern, fmt in _DATE_PATTERNS:
            match = re.search(pattern, fragment)
            if not match:
                continue
            try:
                parsed = dt.datetime.strptime(match.group(0), fmt).date()
            except ValueError:
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), parsed, _is_ambiguous(match.group(0)))

        if best is not None:
            return best[1], found.group(0).strip(), found.start(), best[2]
    return None


def _confidence(*, matched_label: str, exact: bool = True) -> float:
    """How much to trust a match.

    A specific label is a strong signal; a generic one is not. This is a stated
    heuristic rather than a measured probability, and it is named as such so
    nobody reads 0.9 as "ninety per cent of the time this is right".
    """
    base = 0.9 if len(matched_label) > 12 else 0.78
    return base if exact else base - 0.2


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def _extract_lease(text: str) -> tuple[list[Suggestion], list[str]]:
    """Read the fields a lease abstract actually needs."""
    suggestions: list[Suggestion] = []
    missing: list[str] = []

    rent = _money_near(text, ("monthly rent", "base rent", "rent amount", "rent:"))
    if rent:
        amount, evidence, offset = rent
        suggestions.append(
            Suggestion(
                field="rent_amount",
                value=quantize_money(amount),
                confidence=_confidence(matched_label=evidence),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("rent_amount")

    deposit = _money_near(text, ("security deposit", "deposit amount", "deposit:"))
    if deposit:
        amount, evidence, offset = deposit
        suggestions.append(
            Suggestion(
                field="security_deposit",
                value=quantize_money(amount),
                confidence=_confidence(matched_label=evidence),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("security_deposit")

    start = _date_near(text, ("commencement date", "lease start", "term begins", "start date"))
    if start:
        value, evidence, offset, ambiguous = start
        suggestions.append(
            Suggestion(
                field="start_date",
                value=value,
                confidence=_confidence(matched_label=evidence, exact=not ambiguous),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("start_date")

    end = _date_near(text, ("expiration date", "lease end", "term ends", "end date"))
    if end:
        value, evidence, offset, ambiguous = end
        suggestions.append(
            Suggestion(
                field="end_date",
                value=value,
                confidence=_confidence(matched_label=evidence, exact=not ambiguous),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("end_date")

    return suggestions, missing


def _extract_invoice(text: str) -> tuple[list[Suggestion], list[str]]:
    """Read a vendor invoice: number, date, and total."""
    suggestions: list[Suggestion] = []
    missing: list[str] = []

    number = re.search(
        r"invoice\s*(?:#|no\.?|number)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,30})",
        text,
        re.IGNORECASE,
    )
    if number:
        suggestions.append(
            Suggestion(
                field="vendor_invoice_number",
                value=number.group(1).strip(),
                confidence=0.85,
                evidence=number.group(0).strip(),
                offset=number.start(),
            )
        )
    else:
        missing.append("vendor_invoice_number")

    total = _money_near(text, ("amount due", "total due", "balance due", "invoice total", "total"))
    if total:
        amount, evidence, offset = total
        # "Total" alone is weak: subtotal, tax, and total all match it, and
        # picking the wrong one pays the wrong amount.
        weak = evidence.lower().lstrip().startswith("total")
        suggestions.append(
            Suggestion(
                field="total",
                value=quantize_money(amount),
                confidence=0.6 if weak else _confidence(matched_label=evidence),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("total")

    issued = _date_near(text, ("invoice date", "date of invoice", "issued"))
    if issued:
        value, evidence, offset, ambiguous = issued
        suggestions.append(
            Suggestion(
                field="bill_date",
                value=value,
                confidence=_confidence(matched_label=evidence, exact=not ambiguous),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("bill_date")

    due = _date_near(text, ("due date", "payment due", "due by"))
    if due:
        value, evidence, offset, ambiguous = due
        suggestions.append(
            Suggestion(
                field="due_date",
                value=value,
                confidence=_confidence(matched_label=evidence, exact=not ambiguous),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("due_date")

    return suggestions, missing


def _extract_certificate(text: str) -> tuple[list[Suggestion], list[str]]:
    """Read an insurance certificate: who, what policy, and until when."""
    suggestions: list[Suggestion] = []
    missing: list[str] = []

    policy = re.search(
        r"policy\s*(?:#|no\.?|number)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,30})",
        text,
        re.IGNORECASE,
    )
    if policy:
        suggestions.append(
            Suggestion(
                field="policy_number",
                value=policy.group(1).strip(),
                confidence=0.85,
                evidence=policy.group(0).strip(),
                offset=policy.start(),
            )
        )
    else:
        missing.append("policy_number")

    expiry = _date_near(text, ("expiration date", "expiry date", "policy expires", "valid until"))
    if expiry:
        value, evidence, offset, ambiguous = expiry
        suggestions.append(
            Suggestion(
                field="expires_on",
                value=value,
                confidence=_confidence(matched_label=evidence, exact=not ambiguous),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("expires_on")

    coverage = _money_near(text, ("each occurrence", "general aggregate", "coverage limit"))
    if coverage:
        amount, evidence, offset = coverage
        suggestions.append(
            Suggestion(
                field="coverage_amount",
                value=quantize_money(amount),
                confidence=_confidence(matched_label=evidence),
                evidence=evidence,
                offset=offset,
            )
        )
    else:
        missing.append("coverage_amount")

    return suggestions, missing


EXTRACTORS: dict[str, Callable[[str], tuple[list[Suggestion], list[str]]]] = {
    "lease": _extract_lease,
    "invoice": _extract_invoice,
    "insurance_certificate": _extract_certificate,
}


def known_extractors() -> list[str]:
    return sorted(EXTRACTORS)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


def extract(
    text: str,
    *,
    kind: str,
    document: Document | None = None,
) -> Extraction:
    """Read a document. Writes nothing, and asserts nothing.

    The return value is a set of suggestions. Whether any of them becomes a
    fact is :func:`accept_suggestion`, which requires a person.
    """
    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        raise ValidationFailed(
            f"No extractor for {kind!r}. Available: {', '.join(known_extractors())}."
        )

    body = (text or "")[:MAX_TEXT_CHARS]
    suggestions, missing = extractor(body)

    result = Extraction(
        document_id=document.id if document else None,
        kind=kind,
        suggestions=suggestions,
        missing=missing,
    )
    log.info(
        "document extracted",
        extra={
            "event": "extraction.completed",
            "kind": kind,
            "found": len(suggestions),
            "missing": len(missing),
            "needs_review": len(result.needs_review),
        },
    )
    return result


def accept_suggestion(
    session: Session,
    *,
    extraction: Extraction,
    field_name: str,
    accepted_by_id: str,
    org_id: str,
    value: Any = None,
) -> Suggestion:
    """Turn a suggestion into a decision somebody made.

    This is the only path from extracted text to a value the system will act
    on, and it is attributed and audited - so "why does it say this?" has an
    answer that is a name and a sentence rather than a shrug.

    ``value`` lets the reviewer correct the reading rather than only accept or
    reject it, because the common case is that the extractor found the right
    field and misread a digit.
    """
    if not accepted_by_id:
        raise BusinessRuleViolation(
            "An extracted value becomes a fact only when a person accepts it."
        )

    suggestion = extraction.by_field(field_name)
    if suggestion is None:
        raise ValidationFailed(f"No suggestion for {field_name!r} in this extraction.")
    if suggestion.rejected_at is not None:
        raise BusinessRuleViolation("That suggestion has already been rejected.")

    corrected = value is not None and value != suggestion.value
    if value is not None:
        suggestion.value = value

    suggestion.accepted_at = utcnow()
    suggestion.accepted_by_id = accepted_by_id

    record_audit_event(
        action=AuditAction.DOCUMENT_SHARED,
        resource_type="Document",
        resource_id=extraction.document_id,
        resource_label=f"{extraction.kind}:{field_name}",
        severity=AuditSeverity.NOTICE,
        payload={
            "field": field_name,
            "value": str(suggestion.value),
            "confidence": suggestion.confidence,
            "evidence": suggestion.evidence[:255],
            "corrected_by_reviewer": corrected,
        },
        reason="An extracted value was accepted by a person.",
        org_id=org_id,
        actor_id=accepted_by_id,
        session=session,
    )
    return suggestion


def reject_suggestion(
    extraction: Extraction, *, field_name: str, rejected_by_id: str
) -> Suggestion:
    """Discard a reading. Recorded so a bad extractor is visible over time."""
    suggestion = extraction.by_field(field_name)
    if suggestion is None:
        raise ValidationFailed(f"No suggestion for {field_name!r} in this extraction.")
    if suggestion.accepted_at is not None:
        raise BusinessRuleViolation("That suggestion has already been accepted.")

    suggestion.rejected_at = utcnow()
    log.info(
        "extracted value rejected",
        extra={
            "event": "extraction.rejected",
            "field": field_name,
            "confidence": suggestion.confidence,
            "kind": extraction.kind,
        },
    )
    return suggestion


def accepted_values(extraction: Extraction) -> dict[str, Any]:
    """Only what a person signed off. The safe thing to act on."""
    return {
        suggestion.field: suggestion.value
        for suggestion in extraction.suggestions
        if suggestion.accepted_at is not None
    }
