"""The condition language, and the restricted interpreter that evaluates it.

An operations manager writes conditions; they arrive as JSON from the database.
So the interpreter is the security boundary, and it is built on three rules:

* **No evaluation of user text as code.** There is no ``eval``, no ``exec``, no
  format-string interpolation. A condition is a tree of dictionaries walked by a
  fixed set of comparison functions.
* **No attribute access.** Fields resolve through dictionary keys and list
  indices only. There is no path a rule author can take from a payload to an
  object, and therefore no path to ``__class__`` and out of the sandbox.
* **Bounded work.** Depth, node count, and value size are capped, and there is
  no regular-expression operator - a pattern like ``(a+)+b`` is a denial of
  service that looks like a typo, so the language simply does not offer one.
  ``starts_with``, ``ends_with``, and ``contains`` cover what rules actually
  need, in linear time.

Anything unrecognised - an unknown operator, a malformed node - is a validation
error at save time and a *false* at evaluation time. A rule that cannot be
understood never fires.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.errors import ValidationFailed

__all__ = [
    "MAX_CONDITION_DEPTH",
    "MAX_CONDITION_NODES",
    "OPERATORS",
    "ConditionResult",
    "evaluate_conditions",
    "resolve_field",
    "validate_conditions",
]

#: A rule author has no business nesting deeper than this, and an attacker who
#: does should meet a limit rather than a RecursionError.
MAX_CONDITION_DEPTH = 8
MAX_CONDITION_NODES = 100
MAX_FIELD_PATH_SEGMENTS = 8
MAX_STRING_OPERAND = 2_000

#: Group keys combine child conditions; everything else is a leaf comparison.
GROUP_KEYS = ("all", "any", "none")

#: Operators taking no ``value`` at all.
UNARY_OPERATORS = frozenset({"is_null", "is_not_null", "is_true", "is_false"})

#: Operators whose ``value`` must be a list.
SEQUENCE_OPERATORS = frozenset({"in", "not_in"})


@dataclass(frozen=True)
class ConditionResult:
    """The verdict, plus why - so a skipped run can say what did not match."""

    matched: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------

_MISSING = object()


def resolve_field(payload: Any, path: str) -> Any:
    """Walk a dotted path through dictionaries and lists.

    Returns ``None`` for anything absent, which is what makes ``is_null`` the
    natural way to ask "was this field present?". Deliberately does *not* use
    ``getattr``: a condition can only ever see data the caller put in the
    payload, never the objects behind it.
    """
    segments = path.split(".")
    if len(segments) > MAX_FIELD_PATH_SEGMENTS:
        return None

    current: Any = payload
    for segment in segments:
        if isinstance(current, dict):
            current = current.get(segment, _MISSING)
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is _MISSING:
            return None
    return current


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _as_comparable(value: Any) -> Any:
    """Normalise for ordering comparisons.

    Money arrives as a string from JSON and as a ``Decimal`` from the domain;
    ``"1200.00" > "900.00"`` is false as a string comparison and true as a
    number, and that difference is somebody's rent. Numeric-looking values are
    therefore compared numerically.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    if isinstance(value, (dt.datetime, dt.date)):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return value
    return value


def _compare(left: Any, right: Any, comparison: str) -> bool:
    """Ordered comparison that returns False rather than raising on mismatch."""
    a, b = _as_comparable(left), _as_comparable(right)
    if isinstance(a, Decimal) != isinstance(b, Decimal):
        return False
    try:
        if comparison == "gt":
            return bool(a > b)
        if comparison == "gte":
            return bool(a >= b)
        if comparison == "lt":
            return bool(a < b)
        return bool(a <= b)
    except TypeError:
        # Comparing a date to a string is a rule-authoring mistake, not a crash.
        return False


def _equal(left: Any, right: Any) -> bool:
    a, b = _as_comparable(left), _as_comparable(right)
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    return left == right


def _text(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


#: The whole operator vocabulary. Anything absent here is refused at save time.
OPERATORS: dict[str, Any] = {
    "eq": _equal,
    "ne": lambda left, right: not _equal(left, right),
    "gt": lambda left, right: _compare(left, right, "gt"),
    "gte": lambda left, right: _compare(left, right, "gte"),
    "lt": lambda left, right: _compare(left, right, "lt"),
    "lte": lambda left, right: _compare(left, right, "lte"),
    "in": lambda left, right: any(_equal(left, item) for item in right),
    "not_in": lambda left, right: not any(_equal(left, item) for item in right),
    "contains": lambda left, right: (
        any(_equal(item, right) for item in left)
        if isinstance(left, (list, tuple))
        else _text(right).lower() in _text(left).lower()
    ),
    "not_contains": lambda left, right: not OPERATORS["contains"](left, right),
    "starts_with": lambda left, right: _text(left).lower().startswith(_text(right).lower()),
    "ends_with": lambda left, right: _text(left).lower().endswith(_text(right).lower()),
    "is_null": lambda left, _: left is None,
    "is_not_null": lambda left, _: left is not None,
    "is_true": lambda left, _: left is True,
    "is_false": lambda left, _: left is False,
    # An alias for "eq" that reads correctly on a status-change event.
    "changed_to": _equal,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_conditions(conditions: Any) -> list[dict[str, Any]]:
    """Check a condition tree before it is stored.

    Rejecting at save time is what makes evaluation cheap and total: by the time
    a rule runs, every operator in it is known to exist.
    """
    if conditions in (None, [], {}):
        return []
    if not isinstance(conditions, list):
        raise ValidationFailed("Conditions must be a list of condition nodes.")

    counter = _NodeCounter()
    for node in conditions:
        _validate_node(node, depth=1, counter=counter)
    return conditions


class _NodeCounter:
    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def tick(self) -> None:
        self.count += 1
        if self.count > MAX_CONDITION_NODES:
            raise ValidationFailed(
                f"A rule may not contain more than {MAX_CONDITION_NODES} conditions."
            )


def _validate_node(node: Any, *, depth: int, counter: _NodeCounter) -> None:
    counter.tick()
    if depth > MAX_CONDITION_DEPTH:
        raise ValidationFailed(f"Conditions may not nest deeper than {MAX_CONDITION_DEPTH} levels.")
    if not isinstance(node, dict):
        raise ValidationFailed("Each condition must be an object.")

    group = _group_key(node)
    if group is not None:
        children = node[group]
        if not isinstance(children, list) or not children:
            raise ValidationFailed(f"'{group}' must contain at least one condition.")
        for child in children:
            _validate_node(child, depth=depth + 1, counter=counter)
        return

    field = node.get("field")
    if not isinstance(field, str) or not field:
        raise ValidationFailed("Each condition needs a 'field'.")
    if len(field.split(".")) > MAX_FIELD_PATH_SEGMENTS:
        raise ValidationFailed(f"Field path {field!r} is too deep.")

    op = node.get("op")
    if op not in OPERATORS:
        # Fail closed and name the alternatives, so the mistake is fixable.
        raise ValidationFailed(
            f"Unknown condition operator {op!r}. Supported: {', '.join(sorted(OPERATORS))}."
        )

    if op in UNARY_OPERATORS:
        return
    if "value" not in node:
        raise ValidationFailed(f"Operator '{op}' needs a 'value'.")

    value = node["value"]
    if op in SEQUENCE_OPERATORS:
        if not isinstance(value, list):
            raise ValidationFailed(f"Operator '{op}' needs a list value.")
        if len(value) > MAX_CONDITION_NODES:
            raise ValidationFailed(f"Operator '{op}' accepts at most {MAX_CONDITION_NODES} items.")
    if isinstance(value, str) and len(value) > MAX_STRING_OPERAND:
        raise ValidationFailed("A condition value is too long.")


def _group_key(node: dict[str, Any]) -> str | None:
    present = [key for key in GROUP_KEYS if key in node]
    if len(present) > 1:
        raise ValidationFailed("A condition group may use only one of 'all', 'any', or 'none'.")
    return present[0] if present else None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_conditions(conditions: Any, payload: dict[str, Any]) -> ConditionResult:
    """Evaluate a validated condition tree against an event payload.

    Top-level conditions are ANDed, and the first one that fails is named in the
    reason - which is the difference between "the rule did not fire" and "the
    rule did not fire because priority was 'normal'".
    """
    if not conditions:
        return ConditionResult(True)
    if not isinstance(conditions, list):
        return ConditionResult(False, "conditions are malformed")

    for node in conditions:
        result = _evaluate_node(node, payload, depth=1)
        if not result.matched:
            return result
    return ConditionResult(True)


def _evaluate_node(node: Any, payload: dict[str, Any], *, depth: int) -> ConditionResult:
    if depth > MAX_CONDITION_DEPTH or not isinstance(node, dict):
        return ConditionResult(False, "conditions are malformed")

    try:
        group = _group_key(node)
    except ValidationFailed:
        return ConditionResult(False, "conditions are malformed")

    if group is not None:
        children = node[group]
        if not isinstance(children, list) or not children:
            return ConditionResult(False, "conditions are malformed")
        results = [_evaluate_node(child, payload, depth=depth + 1) for child in children]
        if group == "all":
            failed = next((r for r in results if not r.matched), None)
            return failed or ConditionResult(True)
        if group == "any":
            if any(r.matched for r in results):
                return ConditionResult(True)
            return ConditionResult(False, "no condition in 'any' matched")
        # none
        if any(r.matched for r in results):
            return ConditionResult(False, "a condition in 'none' matched")
        return ConditionResult(True)

    field = node.get("field")
    op = node.get("op")
    if not isinstance(field, str) or op not in OPERATORS:
        # An operator that vanished between save and run - a downgrade, say -
        # must not be treated as satisfied.
        return ConditionResult(False, f"unsupported operator {op!r}")

    actual = resolve_field(payload, field)
    expected = node.get("value")
    try:
        matched = bool(OPERATORS[op](actual, expected))
    except Exception:  # noqa: BLE001 - a bad comparison is a false, never a crash
        return ConditionResult(False, f"{field} could not be compared")

    if matched:
        return ConditionResult(True)
    return ConditionResult(False, f"{field} {op} {expected!r} was not satisfied")
