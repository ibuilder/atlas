"""The space hierarchy: where things physically are.

A property is a tree - site, building, floor, unit, room, riser - and every
useful question about a building is a question about that tree. "What is
downstream of this shut-off valve?" "Which units does this riser serve?" "What
did we spend on this floor?" None of them are answerable from a flat list of
rooms with a text label.

Two properties are enforced rather than assumed.

**The tree is a tree.** A space cannot become its own ancestor. Without that
check a single mis-set parent turns every traversal into an infinite loop, and
it is exactly the kind of edit a bulk import makes.

**A space belongs to one property.** Re-parenting across properties would put a
room in a building it is not in, and every roll-up above it would then be
wrong in a way nobody notices until a cost report is questioned.

External geometry references are kept opaque on purpose. An IFC GUID, a room
identifier from a laser scan, and a coordinate reference are all just strings
here; interpreting them belongs to whatever system produced them, and pretending
otherwise would bake one vendor's model into the schema.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.asset_graph import Asset, AssetStatus, Space, SpaceKind

__all__ = [
    "MAX_DEPTH",
    "SpaceNode",
    "ancestors",
    "assets_in",
    "create_space",
    "descendants",
    "link_geometry",
    "move_space",
    "rolled_up_area",
    "space_tree",
]

log = get_logger("services.assets.spaces")

ZERO = Decimal("0")

#: Site, building, floor, wing, unit, room, fixture. Seven is generous; past it
#: something has gone wrong rather than got detailed.
MAX_DEPTH = 12


@dataclass
class SpaceNode:
    """One space and everything under it."""

    space: Space
    children: list[SpaceNode] = field(default_factory=list)

    @property
    def depth(self) -> int:
        return 1 + max((child.depth for child in self.children), default=0)

    def walk(self):  # noqa: ANN201 - a generator of nodes
        yield self
        for child in self.children:
            yield from child.walk()


# ---------------------------------------------------------------------------
# Building the tree
# ---------------------------------------------------------------------------


def _all_spaces(session: Session, *, org_id: str, property_id: str) -> list[Space]:
    return list(
        session.execute(
            select(Space)
            .where(
                Space.org_id == org_id,
                Space.property_id == property_id,
                Space.deleted_at.is_(None),
            )
            .order_by(Space.level, Space.code)
        )
        .scalars()
        .all()
    )


def space_tree(session: Session, *, org_id: str, property_id: str) -> list[SpaceNode]:
    """The whole hierarchy for a property, in one query.

    One query and an in-memory assembly rather than a recursive walk: a
    building with two hundred rooms would otherwise be two hundred round trips,
    and the page that shows it is the page people leave open all day.
    """
    spaces = _all_spaces(session, org_id=org_id, property_id=property_id)
    nodes = {space.id: SpaceNode(space=space) for space in spaces}

    roots: list[SpaceNode] = []
    for space in spaces:
        node = nodes[space.id]
        parent = nodes.get(space.parent_space_id) if space.parent_space_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    return roots


def descendants(session: Session, *, space: Space) -> list[Space]:
    """Everything under a space, at any depth.

    The answer to "what does this serve?" - the question a shut-off valve, a
    riser, or a distribution board exists to raise.
    """
    spaces = _all_spaces(session, org_id=space.org_id, property_id=space.property_id)
    by_parent: dict[str | None, list[Space]] = {}
    for candidate in spaces:
        by_parent.setdefault(candidate.parent_space_id, []).append(candidate)

    found: list[Space] = []
    frontier = list(by_parent.get(space.id, []))
    seen = {space.id}
    while frontier:
        current = frontier.pop()
        if current.id in seen:  # pragma: no cover - the cycle guard should prevent this
            continue
        seen.add(current.id)
        found.append(current)
        frontier.extend(by_parent.get(current.id, []))
    return found


def ancestors(session: Session, *, space: Space) -> list[Space]:
    """From the immediate parent upwards. Bounded, so a cycle cannot hang."""
    chain: list[Space] = []
    current = space
    for _ in range(MAX_DEPTH):
        if not current.parent_space_id:
            break
        parent = session.get(Space, current.parent_space_id)
        if parent is None or parent.id in {s.id for s in chain}:
            break
        chain.append(parent)
        current = parent
    return chain


# ---------------------------------------------------------------------------
# Changing the tree
# ---------------------------------------------------------------------------


def create_space(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    code: str,
    name: str,
    kind: SpaceKind = SpaceKind.ROOM,
    parent: Space | None = None,
    building_id: str | None = None,
    unit_id: str | None = None,
    level: int | None = None,
    area_sqft: Decimal | None = None,
    external_reference: str | None = None,
) -> Space:
    """Add a space, optionally under a parent."""
    if not code or not name:
        raise ValidationFailed("A space needs a code and a name.")
    if parent is not None:
        if parent.org_id != org_id or parent.property_id != property_id:
            raise ValidationFailed("A parent space must be in the same property.")
        if len(ancestors(session, space=parent)) + 1 >= MAX_DEPTH:
            raise BusinessRuleViolation(
                f"A space hierarchy may not nest deeper than {MAX_DEPTH} levels."
            )

    space = Space(
        org_id=org_id,
        property_id=property_id,
        parent_space_id=parent.id if parent else None,
        building_id=building_id or (parent.building_id if parent else None),
        unit_id=unit_id or (parent.unit_id if parent else None),
        code=code,
        name=name,
        kind=kind,
        level=level if level is not None else (parent.level if parent else None),
        area_sqft=area_sqft,
        external_reference=external_reference,
    )
    session.add(space)
    session.flush()
    return space


def move_space(session: Session, *, space: Space, new_parent: Space | None) -> Space:
    """Re-parent a space, refusing anything that would break the tree."""
    if new_parent is None:
        space.parent_space_id = None
        session.flush()
        return space

    if new_parent.org_id != space.org_id:
        raise ValidationFailed("A space cannot move to another organization.")
    if new_parent.property_id != space.property_id:
        # A room in a building it is not in makes every roll-up above it wrong,
        # in a way nobody notices until a cost report is questioned.
        raise ValidationFailed("A space cannot move to a different property.")
    if new_parent.id == space.id:
        raise BusinessRuleViolation("A space cannot be its own parent.")

    # The check that keeps traversal terminating. One mis-set parent from a
    # bulk import would otherwise make every walk an infinite loop.
    if any(candidate.id == new_parent.id for candidate in descendants(session, space=space)):
        raise BusinessRuleViolation(
            "That would put a space inside its own subtree. The hierarchy has to stay a tree."
        )

    if len(ancestors(session, space=new_parent)) + 1 >= MAX_DEPTH:
        raise BusinessRuleViolation(
            f"A space hierarchy may not nest deeper than {MAX_DEPTH} levels."
        )

    space.parent_space_id = new_parent.id
    session.flush()
    return space


def link_geometry(
    session: Session,
    *,
    space: Space,
    external_reference: str | None = None,
    geometry: dict | None = None,
) -> Space:
    """Attach an external model reference.

    Kept opaque deliberately. An IFC GUID, a scan room id, and a coordinate
    reference are all just strings here; interpreting them belongs to the
    system that produced them, and doing it here would bake one vendor's model
    into the schema.
    """
    if external_reference is not None:
        space.external_reference = external_reference[:120] or None
    if geometry is not None:
        if not isinstance(geometry, dict):
            raise ValidationFailed("A geometry reference must be an object.")
        space.geometry_ref = geometry
    session.flush()
    return space


# ---------------------------------------------------------------------------
# Roll-ups
# ---------------------------------------------------------------------------


def rolled_up_area(session: Session, *, space: Space) -> Decimal:
    """Total area of a space and everything under it.

    Sums the leaves' own recorded areas including the root's. A parent whose
    area is recorded *and* whose children are recorded would double-count, so
    the convention is that area belongs to the space that has it and parents
    with children are left null.
    """
    total = space.area_sqft or ZERO
    for child in descendants(session, space=space):
        total += child.area_sqft or ZERO
    return Decimal(total)


def assets_in(session: Session, *, space: Space, include_descendants: bool = True) -> list[Asset]:
    """Equipment located in a space, and optionally everything below it."""
    space_ids = [space.id]
    if include_descendants:
        space_ids.extend(child.id for child in descendants(session, space=space))

    return list(
        session.execute(
            select(Asset)
            .where(
                Asset.org_id == space.org_id,
                Asset.space_id.in_(space_ids),
                Asset.deleted_at.is_(None),
                Asset.status != AssetStatus.RETIRED,
            )
            .order_by(Asset.code)
        )
        .scalars()
        .all()
    )


def space_by_code(session: Session, *, org_id: str, property_id: str, code: str) -> Space:
    space = session.execute(
        select(Space).where(
            Space.org_id == org_id,
            Space.property_id == property_id,
            Space.code == code,
            Space.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if space is None:
        raise NotFound(f"No space with code {code!r} in that property.")
    return space


def path_of(session: Session, *, space: Space) -> str:
    """A readable location: "Larkspur / Level 2 / Flat 204 / Kitchen"."""
    chain = list(reversed(ancestors(session, space=space)))
    return " / ".join([node.name for node in chain] + [space.name])
