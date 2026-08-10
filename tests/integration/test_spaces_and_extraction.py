"""Space hierarchy and document extraction.

Two things carry these features: the tree stays a tree, and an extracted value
is never a fact until a person says so.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.models.asset_graph import Asset, AssetCategory, AssetStatus, SpaceKind
from app.services.assets.spaces import (
    MAX_DEPTH,
    ancestors,
    assets_in,
    create_space,
    descendants,
    link_geometry,
    move_space,
    path_of,
    rolled_up_area,
    space_by_code,
    space_tree,
)
from app.services.documents.extraction import (
    REVIEW_THRESHOLD,
    accept_suggestion,
    accepted_values,
    extract,
    known_extractors,
    reject_suggestion,
)

pytestmark = pytest.mark.integration

REVIEWER = "019fea00-0000-7000-8000-0000000000c1"


@pytest.fixture()
def db_second_property(db, org, scope):
    from app.models.org import Property, PropertyType

    record = Property(
        org_id=org.id,
        code="TST2",
        name="Second House",
        property_type=PropertyType.RESIDENTIAL_MULTI,
        address_line1="2 Test Way",
        city="Testville",
        region="TS",
        postal_code="00002",
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def building(db, org, scope, property_record):
    """Larkspur / Level 2 / Flat 204 / Kitchen, plus a riser."""
    site = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="SITE",
        name="Larkspur",
        kind=SpaceKind.COMMON_AREA,
    )
    level = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="L2",
        name="Level 2",
        kind=SpaceKind.CIRCULATION,
        parent=site,
        level=2,
    )
    flat = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="204",
        name="Flat 204",
        kind=SpaceKind.ROOM,
        parent=level,
        area_sqft=Decimal("780.00"),
    )
    kitchen = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="204-K",
        name="Kitchen",
        kind=SpaceKind.ROOM,
        parent=flat,
        area_sqft=Decimal("120.00"),
    )
    riser = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="RISER-A",
        name="Riser A",
        kind=SpaceKind.MECHANICAL,
        parent=site,
    )
    db.session.commit()
    return {"site": site, "level": level, "flat": flat, "kitchen": kitchen, "riser": riser}


# ------------------------------------------------------------------- the tree


def test_the_tree_assembles_from_one_query(db, org, scope, property_record, building):
    roots = space_tree(db.session, org_id=org.id, property_id=property_record.id)

    assert len(roots) == 1
    assert roots[0].space.code == "SITE"
    assert roots[0].depth == 4
    assert {node.space.code for node in roots[0].walk()} == {
        "SITE",
        "L2",
        "204",
        "204-K",
        "RISER-A",
    }


def test_descendants_answers_what_this_serves(db, org, scope, building):
    """The question a riser, a valve, or a distribution board exists to raise."""
    below = descendants(db.session, space=building["level"])
    assert {space.code for space in below} == {"204", "204-K"}


def test_ancestors_run_upwards(db, org, scope, building):
    chain = ancestors(db.session, space=building["kitchen"])
    assert [space.code for space in chain] == ["204", "L2", "SITE"]


def test_a_readable_path(db, org, scope, building):
    assert path_of(db.session, space=building["kitchen"]) == (
        "Larkspur / Level 2 / Flat 204 / Kitchen"
    )


def test_a_child_inherits_its_parents_placement(db, org, scope, building):
    assert building["kitchen"].level == 2


# ----------------------------------------------------------------- integrity


def test_a_space_cannot_become_its_own_parent(db, org, scope, building):
    with pytest.raises(BusinessRuleViolation):
        move_space(db.session, space=building["flat"], new_parent=building["flat"])


def test_a_space_cannot_move_inside_its_own_subtree(db, org, scope, building):
    """One mis-set parent would otherwise make every traversal an infinite loop."""
    with pytest.raises(BusinessRuleViolation) as exc:
        move_space(db.session, space=building["level"], new_parent=building["kitchen"])
    assert "own subtree" in str(exc.value)


def test_a_space_cannot_move_to_another_property(db, org, scope, building, db_second_property):
    """A room in a building it is not in makes every roll-up above it wrong."""
    other = create_space(
        db.session,
        org_id=org.id,
        property_id=db_second_property.id,
        code="OTHER",
        name="Other site",
    )
    db.session.commit()

    with pytest.raises(ValidationFailed):
        move_space(db.session, space=building["kitchen"], new_parent=other)


def test_a_legitimate_move_works(db, org, scope, building):
    move_space(db.session, space=building["kitchen"], new_parent=building["level"])
    db.session.commit()

    assert building["kitchen"].parent_space_id == building["level"].id
    assert {s.code for s in descendants(db.session, space=building["flat"])} == set()


def test_a_space_can_be_lifted_to_the_root(db, org, scope, building):
    move_space(db.session, space=building["flat"], new_parent=None)
    db.session.commit()

    roots = space_tree(db.session, org_id=org.id, property_id=building["flat"].property_id)
    assert {node.space.code for node in roots} == {"SITE", "204"}


def test_the_hierarchy_has_a_depth_limit(db, org, scope, property_record):
    """It has to stop somewhere, and it has to stop by refusing."""
    parent = None
    created = 0
    with pytest.raises(BusinessRuleViolation):
        for level in range(MAX_DEPTH + 3):
            parent = create_space(
                db.session,
                org_id=org.id,
                property_id=property_record.id,
                code=f"D{level}",
                name=f"Depth {level}",
                parent=parent,
            )
            created += 1
    db.session.commit()

    assert created < MAX_DEPTH + 3


# ------------------------------------------------------------------ roll-ups


def test_area_rolls_up(db, org, scope, building):
    assert rolled_up_area(db.session, space=building["level"]) == Decimal("900.00")


def test_assets_roll_up_through_the_tree(db, org, scope, building, property_record):
    db.session.add(
        Asset(
            org_id=org.id,
            code="EXTRACT-1",
            name="Extractor fan",
            category=AssetCategory.HVAC,
            property_id=property_record.id,
            space_id=building["kitchen"].id,
        )
    )
    db.session.commit()

    assert [a.code for a in assets_in(db.session, space=building["site"])] == ["EXTRACT-1"]
    assert assets_in(db.session, space=building["riser"]) == []


def test_a_retired_asset_is_not_located_anywhere(db, org, scope, building, property_record):
    asset = Asset(
        org_id=org.id,
        code="OLD-1",
        name="Old plant",
        category=AssetCategory.HVAC,
        property_id=property_record.id,
        space_id=building["kitchen"].id,
        status=AssetStatus.RETIRED,
    )
    db.session.add(asset)
    db.session.commit()

    assert assets_in(db.session, space=building["site"]) == []


# ------------------------------------------------------------------ geometry


def test_an_external_reference_is_stored_opaquely(db, org, scope, building):
    """Interpreting an IFC GUID is the producing system's job, not ours."""
    link_geometry(
        db.session,
        space=building["kitchen"],
        external_reference="3vB2N$1234567890abcdefg",
        geometry={"source": "ifc", "storey": "L2", "bbox": [0, 0, 10, 12]},
    )
    db.session.commit()

    assert building["kitchen"].external_reference.startswith("3vB2N$")
    assert building["kitchen"].geometry_ref["source"] == "ifc"


def test_a_non_object_geometry_reference_is_refused(db, org, scope, building):
    with pytest.raises(ValidationFailed):
        link_geometry(db.session, space=building["kitchen"], geometry="not an object")


def test_a_space_is_findable_by_code(db, org, scope, building, property_record):
    found = space_by_code(db.session, org_id=org.id, property_id=property_record.id, code="RISER-A")
    assert found.id == building["riser"].id

    with pytest.raises(NotFound):
        space_by_code(db.session, org_id=org.id, property_id=property_record.id, code="NOPE")


# =========================================================== extraction ====

LEASE = """
RESIDENTIAL TENANCY AGREEMENT

Commencement date: 2026-04-01
Expiration date: 2027-03-31

Monthly rent: $3,100.00 payable in advance on the first day of each month.
Security deposit: $4,650.00 held in trust.
"""

INVOICE = """
ACME PLUMBING LTD
Invoice Number: INV-20260412
Invoice date: 2026-04-12
Due date: 2026-05-12

Labour                         850.00
Parts                          312.50
Amount due: $1,162.50
"""

CERTIFICATE = """
CERTIFICATE OF LIABILITY INSURANCE
Policy Number: GL-889-2261
Expiration date: 2027-01-31
Each occurrence: $2,000,000
"""


def test_the_catalogue_lists_its_extractors():
    assert set(known_extractors()) == {"lease", "invoice", "insurance_certificate"}


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValidationFailed):
        extract("anything", kind="tea_leaves")


def test_a_lease_yields_the_fields_an_abstract_needs():
    result = extract(LEASE, kind="lease")

    assert result.by_field("rent_amount").value == Decimal("3100.0000")
    assert result.by_field("security_deposit").value == Decimal("4650.0000")
    assert result.by_field("start_date").value == dt.date(2026, 4, 1)
    assert result.by_field("end_date").value == dt.date(2027, 3, 31)
    assert result.missing == []


def test_every_suggestion_carries_its_evidence():
    """The reviewer checks the document rather than trusting the number."""
    result = extract(LEASE, kind="lease")
    rent = result.by_field("rent_amount")

    assert "Monthly rent" in rent.evidence
    assert "3,100.00" in rent.evidence
    assert rent.offset is not None


def test_a_missing_field_is_reported_not_omitted():
    """A missing field is as wrong as a bad one and much easier to overlook."""
    result = extract("A lease with no numbers in it at all.", kind="lease")

    assert result.suggestions == []
    assert set(result.missing) == {
        "rent_amount",
        "security_deposit",
        "start_date",
        "end_date",
    }


def test_an_invoice_yields_its_totals():
    result = extract(INVOICE, kind="invoice")

    assert result.by_field("vendor_invoice_number").value == "INV-20260412"
    assert result.by_field("total").value == Decimal("1162.5000")
    assert result.by_field("bill_date").value == dt.date(2026, 4, 12)
    assert result.by_field("due_date").value == dt.date(2026, 5, 12)


def test_a_weak_label_gets_a_low_confidence():
    """ "Total" alone matches subtotal and tax too, and picking wrong pays wrong."""
    weak = "ACME LTD\nInvoice no: X-1\nTotal 400.00\n"
    result = extract(weak, kind="invoice")

    total = result.by_field("total")
    assert total.confidence < REVIEW_THRESHOLD
    assert total.needs_review is True
    assert total in result.needs_review


def test_an_ambiguous_slash_date_is_flagged_rather_than_guessed():
    """12/04/2026 is 12 April here and 4 December in the US. Guessing books a
    payment a month out, so the reading survives with a confidence that forces
    somebody to look."""
    text = "Invoice no: X-9\nInvoice date: 12/04/2026\nAmount due: $100.00\n"
    result = extract(text, kind="invoice")

    bill_date = result.by_field("bill_date")
    assert bill_date is not None
    assert bill_date.needs_review is True
    assert bill_date in result.needs_review


def test_an_unambiguous_slash_date_is_trusted():
    """25/12 can only be a day and a month, so there is nothing to review."""
    text = "Invoice no: X-9\nInvoice date: 12/25/2026\nAmount due: $100.00\n"
    result = extract(text, kind="invoice")

    bill_date = result.by_field("bill_date")
    assert bill_date.value == dt.date(2026, 12, 25)
    assert bill_date.needs_review is False


def test_a_certificate_yields_its_expiry():
    result = extract(CERTIFICATE, kind="insurance_certificate")

    assert result.by_field("policy_number").value == "GL-889-2261"
    assert result.by_field("expires_on").value == dt.date(2027, 1, 31)
    assert result.by_field("coverage_amount").value == Decimal("2000000.0000")


def test_a_clean_extraction_reports_itself_confident():
    assert extract(LEASE, kind="lease").is_confident is True


def test_an_extraction_with_a_gap_is_not_confident():
    assert extract("Monthly rent: $3,100.00", kind="lease").is_confident is False


# ----------------------------------------------------------- accept, reject


def test_nothing_is_a_fact_until_a_person_accepts_it(db, org, scope):
    """The property the whole design exists for."""
    result = extract(LEASE, kind="lease")

    assert accepted_values(result) == {}
    assert all(suggestion.is_pending for suggestion in result.suggestions)


def test_accepting_attributes_the_decision(db, org, scope):
    result = extract(LEASE, kind="lease")
    accepted = accept_suggestion(
        db.session,
        extraction=result,
        field_name="rent_amount",
        accepted_by_id=REVIEWER,
        org_id=org.id,
    )
    db.session.commit()

    assert accepted.accepted_by_id == REVIEWER
    assert accepted.accepted_at is not None
    assert accepted_values(result) == {"rent_amount": Decimal("3100.0000")}


def test_an_unattributed_acceptance_is_refused(db, org, scope):
    result = extract(LEASE, kind="lease")
    with pytest.raises(BusinessRuleViolation):
        accept_suggestion(
            db.session,
            extraction=result,
            field_name="rent_amount",
            accepted_by_id="",
            org_id=org.id,
        )


def test_a_reviewer_can_correct_a_misread_value(db, org, scope):
    """The common case: the right field, a wrong digit."""
    result = extract(LEASE, kind="lease")
    accept_suggestion(
        db.session,
        extraction=result,
        field_name="rent_amount",
        accepted_by_id=REVIEWER,
        org_id=org.id,
        value=Decimal("3150.00"),
    )
    db.session.commit()

    assert accepted_values(result)["rent_amount"] == Decimal("3150.00")


def test_the_acceptance_is_audited_with_its_evidence(db, org, scope):
    """ "Why does it say 3,100?" answered with a name and a sentence."""
    from app.models.audit import AuditEvent

    result = extract(LEASE, kind="lease")
    accept_suggestion(
        db.session,
        extraction=result,
        field_name="rent_amount",
        accepted_by_id=REVIEWER,
        org_id=org.id,
    )
    db.session.commit()

    event = (
        db.session.query(AuditEvent).filter(AuditEvent.resource_label == "lease:rent_amount").one()
    )
    assert event.actor_id == REVIEWER
    assert "Monthly rent" in event.payload["evidence"]
    assert event.payload["corrected_by_reviewer"] is False


def test_a_correction_is_flagged_in_the_audit(db, org, scope):
    from app.models.audit import AuditEvent

    result = extract(LEASE, kind="lease")
    accept_suggestion(
        db.session,
        extraction=result,
        field_name="rent_amount",
        accepted_by_id=REVIEWER,
        org_id=org.id,
        value=Decimal("3150.00"),
    )
    db.session.commit()

    event = (
        db.session.query(AuditEvent).filter(AuditEvent.resource_label == "lease:rent_amount").one()
    )
    assert event.payload["corrected_by_reviewer"] is True


def test_rejecting_removes_it_from_the_accepted_set(db, org, scope):
    result = extract(LEASE, kind="lease")
    reject_suggestion(result, field_name="security_deposit", rejected_by_id=REVIEWER)

    assert accepted_values(result) == {}
    assert result.by_field("security_deposit").is_pending is False


def test_a_rejected_suggestion_cannot_then_be_accepted(db, org, scope):
    result = extract(LEASE, kind="lease")
    reject_suggestion(result, field_name="rent_amount", rejected_by_id=REVIEWER)

    with pytest.raises(BusinessRuleViolation):
        accept_suggestion(
            db.session,
            extraction=result,
            field_name="rent_amount",
            accepted_by_id=REVIEWER,
            org_id=org.id,
        )


def test_an_accepted_suggestion_cannot_then_be_rejected(db, org, scope):
    result = extract(LEASE, kind="lease")
    accept_suggestion(
        db.session,
        extraction=result,
        field_name="rent_amount",
        accepted_by_id=REVIEWER,
        org_id=org.id,
    )
    with pytest.raises(BusinessRuleViolation):
        reject_suggestion(result, field_name="rent_amount", rejected_by_id=REVIEWER)


def test_an_unknown_field_cannot_be_accepted(db, org, scope):
    result = extract(LEASE, kind="lease")
    with pytest.raises(ValidationFailed):
        accept_suggestion(
            db.session,
            extraction=result,
            field_name="not_a_field",
            accepted_by_id=REVIEWER,
            org_id=org.id,
        )


def test_an_enormous_document_is_truncated_not_processed_whole():
    """Past twenty pages it is a scan artefact or an attack."""
    padded = ("x" * 500_000) + "\nMonthly rent: $3,100.00\n"
    result = extract(padded, kind="lease")
    assert result.by_field("rent_amount") is None
