"""Bulk import.

The acceptance case is the workflow that actually happens: upload, half the
rows fail, fix the spreadsheet, upload the whole thing again. That must not
duplicate the half that already landed.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.errors import NotFound, ValidationFailed
from app.models.org import Property, Unit
from app.models.vendor import Vendor
from app.services.imports import (
    apply_plan,
    known_importers,
    plan_import,
    template_for,
)

pytestmark = pytest.mark.integration

PROPERTIES = (
    "code,name,address_line1,city,region,postal_code,property_type,year_built\r\n"
    "LARK,Larkspur Court,12 Larkspur Way,Testville,TS,00001,residential_multi,1998\r\n"
    "OAK,Oakfield House,4 Oak Lane,Testville,TS,00002,residential_single,2004\r\n"
)


def _plan(db, org, resource, text):
    return plan_import(db.session, org_id=org.id, resource=resource, text=text)


# ------------------------------------------------------------------ registry


def test_the_catalogue_lists_its_importers():
    assert set(known_importers()) >= {"properties", "units", "vendors"}


def test_an_unknown_resource_is_refused(db, org, scope):
    with pytest.raises(NotFound):
        _plan(db, org, "unicorns", "a,b\n1,2\n")


def test_a_template_names_the_columns():
    header = template_for("properties")
    assert header.startswith("code,name,address_line1")
    assert "year_built" in header


# ---------------------------------------------------------------- planning


def test_a_plan_reads_only(db, org, scope):
    """Nothing about a spreadsheet from an unknown system earns trust first time."""
    plan = _plan(db, org, "properties", PROPERTIES)
    db.session.commit()

    assert plan.is_valid
    assert plan.creates == 2
    assert plan.updates == 0
    assert db.session.query(Property).count() == 0


def test_applying_a_plan_writes(db, org, scope):
    plan = _plan(db, org, "properties", PROPERTIES)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    assert db.session.query(Property).count() == 2
    lark = db.session.query(Property).filter_by(code="LARK").one()
    assert lark.name == "Larkspur Court"
    assert lark.year_built == 1998


def test_reuploading_the_same_file_changes_nothing(db, org, scope):
    """The acceptance case. Fix the spreadsheet and try again must be safe."""
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    second = _plan(db, org, "properties", PROPERTIES)
    apply_plan(db.session, org_id=org.id, plan=second)
    db.session.commit()

    assert second.creates == 0
    assert second.unchanged == 2
    assert db.session.query(Property).count() == 2


def test_a_corrected_row_updates_rather_than_duplicating(db, org, scope):
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    corrected = PROPERTIES.replace("Larkspur Court", "Larkspur Court North")
    plan = _plan(db, org, "properties", corrected)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    assert plan.updates == 1
    assert plan.unchanged == 1
    assert db.session.query(Property).count() == 2
    assert db.session.query(Property).filter_by(code="LARK").one().name == "Larkspur Court North"


def test_the_plan_says_exactly_what_would_change(db, org, scope):
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    plan = _plan(db, org, "properties", PROPERTIES.replace("1998", "1999"))
    changed = [row for row in plan.rows if row.action == "update"]
    assert len(changed) == 1
    assert changed[0].changes["year_built"] == {"from": 1998, "to": 1999}


# ------------------------------------------------------------------ errors


def test_a_missing_column_is_reported_against_the_header(db, org, scope):
    plan = _plan(db, org, "properties", "code,name\r\nLARK,Larkspur\r\n")
    assert not plan.is_valid
    assert plan.errors[0].row == 1
    assert "address_line1" in plan.errors[0].message


def test_a_bad_value_names_the_row_and_the_column(db, org, scope):
    """The row number is the spreadsheet's, because that is what they see."""
    broken = PROPERTIES.replace(",1998", ",nineteen ninety eight")
    plan = _plan(db, org, "properties", broken)

    assert not plan.is_valid
    assert plan.errors[0].row == 2
    assert "year_built" in plan.errors[0].message


def test_an_unknown_enum_lists_the_valid_options(db, org, scope):
    broken = PROPERTIES.replace("residential_multi", "apartment_block")
    plan = _plan(db, org, "properties", broken)
    assert "residential_multi" in plan.errors[0].message


def test_a_file_with_any_error_is_refused_in_full(db, org, scope):
    """A partial import is worse than a failed one."""
    broken = PROPERTIES.replace(",1998", ",not a year")
    plan = _plan(db, org, "properties", broken)

    with pytest.raises(ValidationFailed) as exc:
        apply_plan(db.session, org_id=org.id, plan=plan)
    assert "not imported" in str(exc.value)
    assert db.session.query(Property).count() == 0


def test_two_rows_claiming_the_same_record_is_an_error(db, org, scope):
    """Which one wins is not our decision to make."""
    duplicated = PROPERTIES + "LARK,Larkspur Again,9 Other St,Testville,TS,00003,,\r\n"
    plan = _plan(db, org, "properties", duplicated)

    assert not plan.is_valid
    assert "row 2" in plan.errors[0].message


def test_a_file_with_no_header_is_refused(db, org, scope):
    with pytest.raises(ValidationFailed):
        _plan(db, org, "properties", "")


def test_blank_rows_are_skipped(db, org, scope):
    plan = _plan(db, org, "properties", PROPERTIES + ",,,,,,,\r\n")
    assert plan.is_valid
    assert plan.creates == 2


def test_several_dates_formats_are_understood(db, org, scope):
    for value in ("2027-03-01", "01/03/2027", "01-Mar-2027"):
        text = f"code,name,compliance_expires_at\r\nV1,Acme,{value}\r\n"
        plan = _plan(db, org, "vendors", text)
        assert plan.is_valid, plan.errors


def test_an_unrecognised_date_is_refused_with_a_hint(db, org, scope):
    text = "code,name,compliance_expires_at\r\nV1,Acme,next Tuesday\r\n"
    plan = _plan(db, org, "vendors", text)
    assert "YYYY-MM-DD" in plan.errors[0].message


# ------------------------------------------------------------------- units


def test_units_resolve_their_property_by_code(db, org, scope):
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    text = (
        "property_code,unit_number,bedrooms,bathrooms,market_rent,status\r\n"
        "LARK,101,2,1.5,3100.00,vacant_ready\r\n"
        "LARK,102,1,1,2400.00,occupied\r\n"
    )
    plan = _plan(db, org, "units", text)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    assert db.session.query(Unit).count() == 2
    unit = db.session.query(Unit).filter_by(unit_number="101").one()
    assert unit.bedrooms == 2
    assert unit.bathrooms == Decimal("1.5")
    assert unit.market_rent == Decimal("3100.0000")


def test_a_unit_for_an_unknown_property_is_refused(db, org, scope):
    text = "property_code,unit_number\r\nNOPE,101\r\n"
    plan = _plan(db, org, "units", text)
    assert "No property with code 'NOPE'" in plan.errors[0].message


def test_units_are_keyed_within_their_property(db, org, scope):
    """Unit 101 exists in every building; the key has to include which."""
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    text = "property_code,unit_number\r\nLARK,101\r\nOAK,101\r\n"
    plan = _plan(db, org, "units", text)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    assert plan.creates == 2
    assert db.session.query(Unit).count() == 2


def test_reuploading_units_updates_in_place(db, org, scope):
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    first = "property_code,unit_number,market_rent\r\nLARK,101,3100.00\r\n"
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "units", first))
    db.session.commit()

    raised = "property_code,unit_number,market_rent\r\nLARK,101,3250.00\r\n"
    plan = _plan(db, org, "units", raised)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    assert plan.updates == 1
    assert db.session.query(Unit).count() == 1
    assert db.session.query(Unit).one().market_rent == Decimal("3250.0000")


# ----------------------------------------------------------------- vendors


def test_vendors_import(db, org, scope):
    text = (
        "code,name,email,phone,compliance_expires_at\r\n"
        "ACME,Acme Plumbing,ops@acme.test,555-0100,2027-03-01\r\n"
    )
    plan = _plan(db, org, "vendors", text)
    apply_plan(db.session, org_id=org.id, plan=plan)
    db.session.commit()

    vendor = db.session.query(Vendor).one()
    assert vendor.name == "Acme Plumbing"
    assert vendor.email == "ops@acme.test"


def test_a_malformed_email_is_refused(db, org, scope):
    text = "code,name,email\r\nACME,Acme,not-an-address\r\n"
    plan = _plan(db, org, "vendors", text)
    assert "not an email address" in plan.errors[0].message


# --------------------------------------------------------------- isolation


def test_an_import_lands_in_one_organization_only(db, org, other_org, scope):
    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    assert db.session.query(Property).filter(Property.org_id == other_org.id).count() == 0


def test_the_import_is_audited(db, org, scope):
    from app.models.audit import AuditEvent

    apply_plan(db.session, org_id=org.id, plan=_plan(db, org, "properties", PROPERTIES))
    db.session.commit()

    event = db.session.query(AuditEvent).filter(AuditEvent.resource_type == "BulkImport").one()
    assert event.payload["created"] == 2
    assert event.payload["resource"] == "properties"
