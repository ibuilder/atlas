"""The permission vocabulary and the system role definitions.

Permissions are named ``<domain>.<action>``. The vocabulary is closed and lives
in code: tenants compose roles from it, but they cannot invent verbs the
authorization engine has never heard of. Every permission is declared once, with
a category for the admin UI and a ``sensitive`` flag that forces a fresh MFA
assertion before it can be exercised.

System roles are the starting set every organization is provisioned with. They
are copyable and adjustable, but not deletable - a tenant that removes its last
administrator role should not be able to lock itself out.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.models.iam import UserType

__all__ = [
    "PERMISSION_CATALOG",
    "SYSTEM_ROLES",
    "Perm",
    "PermissionDef",
    "RoleDef",
    "all_permission_codes",
    "permissions_for_role",
    "sensitive_permission_codes",
]


class Perm:
    """Permission code constants."""

    # -- platform ---------------------------------------------------------
    ORG_READ = "org.read"
    ORG_MANAGE = "org.manage"
    ORG_SETTINGS_MANAGE = "org.settings_manage"
    AUDIT_READ = "audit.read"
    AUDIT_EXPORT = "audit.export"

    # -- identity ---------------------------------------------------------
    USER_READ = "user.read"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DISABLE = "user.disable"
    USER_IMPERSONATE = "user.impersonate"
    ROLE_READ = "role.read"
    ROLE_MANAGE = "role.manage"
    ROLE_ASSIGN = "role.assign"
    API_TOKEN_MANAGE = "api_token.manage"

    # -- portfolio --------------------------------------------------------
    PORTFOLIO_READ = "portfolio.read"
    PORTFOLIO_MANAGE = "portfolio.manage"
    PROPERTY_READ = "property.read"
    PROPERTY_CREATE = "property.create"
    PROPERTY_UPDATE = "property.update"
    PROPERTY_DELETE = "property.delete"
    UNIT_READ = "unit.read"
    UNIT_MANAGE = "unit.manage"
    OWNER_READ = "owner.read"
    OWNER_MANAGE = "owner.manage"
    OWNER_STATEMENT_READ = "owner.statement_read"

    # -- leasing ----------------------------------------------------------
    LEAD_READ = "lead.read"
    LEAD_MANAGE = "lead.manage"
    APPLICATION_READ = "application.read"
    APPLICATION_MANAGE = "application.manage"
    APPLICATION_DECIDE = "application.decide"
    SCREENING_READ = "screening.read"
    SCREENING_ORDER = "screening.order"
    LEASE_READ = "lease.read"
    LEASE_CREATE = "lease.create"
    LEASE_UPDATE = "lease.update"
    LEASE_TERMINATE = "lease.terminate"
    LEASE_RENEW = "lease.renew"

    # -- residents --------------------------------------------------------
    RESIDENT_READ = "resident.read"
    RESIDENT_MANAGE = "resident.manage"
    RESIDENT_PII_READ = "resident.pii_read"
    MESSAGE_READ = "message.read"
    MESSAGE_SEND = "message.send"
    NOTICE_ISSUE = "notice.issue"

    # -- accounting -------------------------------------------------------
    LEDGER_READ = "ledger.read"
    LEDGER_POST = "ledger.post"
    LEDGER_REVERSE = "ledger.reverse"
    ACCOUNT_MANAGE = "account.manage"
    INVOICE_READ = "invoice.read"
    INVOICE_MANAGE = "invoice.manage"
    INVOICE_VOID = "invoice.void"
    PAYMENT_READ = "payment.read"
    PAYMENT_RECORD = "payment.record"
    PAYMENT_REFUND = "payment.refund"
    DEPOSIT_READ = "deposit.read"
    DEPOSIT_COLLECT = "deposit.collect"
    DEPOSIT_RELEASE = "deposit.release"
    BILL_READ = "bill.read"
    BILL_MANAGE = "bill.manage"
    BILL_APPROVE = "bill.approve"
    BILL_PAY = "bill.pay"
    BANK_ACCOUNT_READ = "bank_account.read"
    BANK_ACCOUNT_MANAGE = "bank_account.manage"
    RECONCILIATION_READ = "reconciliation.read"
    RECONCILIATION_MANAGE = "reconciliation.manage"
    PERIOD_CLOSE = "period.close"
    PERIOD_REOPEN = "period.reopen"
    DISTRIBUTION_MANAGE = "distribution.manage"
    DISTRIBUTION_APPROVE = "distribution.approve"

    # -- maintenance ------------------------------------------------------
    REQUEST_READ = "request.read"
    REQUEST_CREATE = "request.create"
    REQUEST_TRIAGE = "request.triage"
    WORK_ORDER_READ = "work_order.read"
    WORK_ORDER_CREATE = "work_order.create"
    WORK_ORDER_UPDATE = "work_order.update"
    WORK_ORDER_ASSIGN = "work_order.assign"
    WORK_ORDER_COMPLETE = "work_order.complete"
    WORK_ORDER_APPROVE = "work_order.approve"
    INSPECTION_READ = "inspection.read"
    INSPECTION_MANAGE = "inspection.manage"
    INSPECTION_PERFORM = "inspection.perform"
    PM_MANAGE = "pm.manage"

    # -- vendors ----------------------------------------------------------
    VENDOR_READ = "vendor.read"
    VENDOR_MANAGE = "vendor.manage"
    VENDOR_COMPLIANCE_MANAGE = "vendor.compliance_manage"

    # -- documents --------------------------------------------------------
    DOCUMENT_READ = "document.read"
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DELETE = "document.delete"
    DOCUMENT_SHARE = "document.share"

    # -- assets -----------------------------------------------------------
    ASSET_READ = "asset.read"
    ASSET_MANAGE = "asset.manage"

    # -- reporting and automation ----------------------------------------
    REPORT_READ = "report.read"
    REPORT_RUN = "report.run"
    REPORT_SCHEDULE = "report.schedule"
    DATA_EXPORT = "data.export"
    AUTOMATION_READ = "automation.read"
    AUTOMATION_MANAGE = "automation.manage"
    AUTOMATION_PUBLISH = "automation.publish"
    APPROVAL_DECIDE = "approval.decide"

    # -- integration ------------------------------------------------------
    INTEGRATION_READ = "integration.read"
    INTEGRATION_MANAGE = "integration.manage"
    WEBHOOK_MANAGE = "webhook.manage"
    IMPORT_RUN = "import.run"


@dataclass(frozen=True)
class PermissionDef:
    code: str
    name: str
    category: str
    description: str = ""
    #: Requires a recent MFA assertion, regardless of which role grants it.
    #: Reserved for actions that move money, change who can move money, or
    #: expose bulk personal data.
    sensitive: bool = False


def _p(
    code: str, name: str, category: str, description: str = "", sensitive: bool = False
) -> PermissionDef:
    return PermissionDef(code, name, category, description, sensitive)


PERMISSION_CATALOG: Final[tuple[PermissionDef, ...]] = (
    # platform
    _p(Perm.ORG_READ, "View organization", "Platform"),
    _p(Perm.ORG_MANAGE, "Manage organization", "Platform", sensitive=True),
    _p(Perm.ORG_SETTINGS_MANAGE, "Manage settings", "Platform", sensitive=True),
    _p(Perm.AUDIT_READ, "View audit trail", "Platform"),
    _p(Perm.AUDIT_EXPORT, "Export audit trail", "Platform", sensitive=True),
    # identity
    _p(Perm.USER_READ, "View users", "Identity"),
    _p(Perm.USER_CREATE, "Invite users", "Identity", sensitive=True),
    _p(Perm.USER_UPDATE, "Edit users", "Identity", sensitive=True),
    _p(Perm.USER_DISABLE, "Disable users", "Identity", sensitive=True),
    _p(
        Perm.USER_IMPERSONATE,
        "Impersonate users",
        "Identity",
        "Support access; every impersonated action is audited as such.",
        sensitive=True,
    ),
    _p(Perm.ROLE_READ, "View roles", "Identity"),
    _p(Perm.ROLE_MANAGE, "Manage roles", "Identity", sensitive=True),
    _p(Perm.ROLE_ASSIGN, "Assign roles", "Identity", sensitive=True),
    _p(Perm.API_TOKEN_MANAGE, "Manage API tokens", "Identity", sensitive=True),
    # portfolio
    _p(Perm.PORTFOLIO_READ, "View portfolios", "Portfolio"),
    _p(Perm.PORTFOLIO_MANAGE, "Manage portfolios", "Portfolio"),
    _p(Perm.PROPERTY_READ, "View properties", "Portfolio"),
    _p(Perm.PROPERTY_CREATE, "Add properties", "Portfolio"),
    _p(Perm.PROPERTY_UPDATE, "Edit properties", "Portfolio"),
    _p(Perm.PROPERTY_DELETE, "Remove properties", "Portfolio", sensitive=True),
    _p(Perm.UNIT_READ, "View units", "Portfolio"),
    _p(Perm.UNIT_MANAGE, "Manage units", "Portfolio"),
    _p(Perm.OWNER_READ, "View owners", "Portfolio"),
    _p(Perm.OWNER_MANAGE, "Manage owners", "Portfolio", sensitive=True),
    _p(Perm.OWNER_STATEMENT_READ, "View owner statements", "Portfolio"),
    # leasing
    _p(Perm.LEAD_READ, "View leads", "Leasing"),
    _p(Perm.LEAD_MANAGE, "Manage leads", "Leasing"),
    _p(Perm.APPLICATION_READ, "View applications", "Leasing"),
    _p(Perm.APPLICATION_MANAGE, "Manage applications", "Leasing"),
    _p(
        Perm.APPLICATION_DECIDE,
        "Approve or deny applications",
        "Leasing",
        "Fair-housing sensitive; every decision requires a recorded reason.",
        sensitive=True,
    ),
    _p(Perm.SCREENING_READ, "View screening results", "Leasing", sensitive=True),
    _p(Perm.SCREENING_ORDER, "Order screening", "Leasing", sensitive=True),
    _p(Perm.LEASE_READ, "View leases", "Leasing"),
    _p(Perm.LEASE_CREATE, "Create leases", "Leasing"),
    _p(Perm.LEASE_UPDATE, "Edit leases", "Leasing"),
    _p(Perm.LEASE_TERMINATE, "Terminate leases", "Leasing", sensitive=True),
    _p(Perm.LEASE_RENEW, "Offer renewals", "Leasing"),
    # residents
    _p(Perm.RESIDENT_READ, "View residents", "Residents"),
    _p(Perm.RESIDENT_MANAGE, "Manage residents", "Residents"),
    _p(
        Perm.RESIDENT_PII_READ,
        "View resident personal data",
        "Residents",
        "Date of birth, government identifiers. Access is individually audited.",
        sensitive=True,
    ),
    _p(Perm.MESSAGE_READ, "Read messages", "Residents"),
    _p(Perm.MESSAGE_SEND, "Send messages", "Residents"),
    _p(Perm.NOTICE_ISSUE, "Issue notices", "Residents", sensitive=True),
    # accounting
    _p(Perm.LEDGER_READ, "View ledger", "Accounting"),
    _p(Perm.LEDGER_POST, "Post journal entries", "Accounting", sensitive=True),
    _p(Perm.LEDGER_REVERSE, "Reverse journal entries", "Accounting", sensitive=True),
    _p(Perm.ACCOUNT_MANAGE, "Manage chart of accounts", "Accounting", sensitive=True),
    _p(Perm.INVOICE_READ, "View invoices", "Accounting"),
    _p(Perm.INVOICE_MANAGE, "Manage invoices", "Accounting"),
    _p(Perm.INVOICE_VOID, "Void invoices", "Accounting", sensitive=True),
    _p(Perm.PAYMENT_READ, "View payments", "Accounting"),
    _p(Perm.PAYMENT_RECORD, "Record payments", "Accounting"),
    _p(Perm.PAYMENT_REFUND, "Refund payments", "Accounting", sensitive=True),
    _p(Perm.DEPOSIT_READ, "View deposits held", "Accounting"),
    _p(Perm.DEPOSIT_COLLECT, "Take deposits into trust", "Accounting"),
    # Money leaving a trust account belongs to somebody else. Split from
    # collecting for the same reason entering a bill is split from paying it.
    _p(Perm.DEPOSIT_RELEASE, "Release deposits from trust", "Accounting", sensitive=True),
    _p(Perm.BILL_READ, "View bills", "Accounting"),
    _p(Perm.BILL_MANAGE, "Manage bills", "Accounting"),
    _p(Perm.BILL_APPROVE, "Approve bills", "Accounting", sensitive=True),
    _p(Perm.BILL_PAY, "Pay bills", "Accounting", sensitive=True),
    _p(Perm.BANK_ACCOUNT_READ, "View bank accounts", "Accounting"),
    _p(
        Perm.BANK_ACCOUNT_MANAGE,
        "Manage bank accounts",
        "Accounting",
        "Classic fraud vector: changes require a second approver.",
        sensitive=True,
    ),
    _p(Perm.RECONCILIATION_READ, "View reconciliations", "Accounting"),
    _p(Perm.RECONCILIATION_MANAGE, "Perform reconciliations", "Accounting"),
    _p(Perm.PERIOD_CLOSE, "Close accounting periods", "Accounting", sensitive=True),
    _p(Perm.PERIOD_REOPEN, "Reopen accounting periods", "Accounting", sensitive=True),
    _p(Perm.DISTRIBUTION_MANAGE, "Prepare owner distributions", "Accounting", sensitive=True),
    _p(Perm.DISTRIBUTION_APPROVE, "Approve owner distributions", "Accounting", sensitive=True),
    # maintenance
    _p(Perm.REQUEST_READ, "View maintenance requests", "Maintenance"),
    _p(Perm.REQUEST_CREATE, "Submit maintenance requests", "Maintenance"),
    _p(Perm.REQUEST_TRIAGE, "Triage requests", "Maintenance"),
    _p(Perm.WORK_ORDER_READ, "View work orders", "Maintenance"),
    _p(Perm.WORK_ORDER_CREATE, "Create work orders", "Maintenance"),
    _p(Perm.WORK_ORDER_UPDATE, "Update work orders", "Maintenance"),
    _p(Perm.WORK_ORDER_ASSIGN, "Assign work orders", "Maintenance"),
    _p(Perm.WORK_ORDER_COMPLETE, "Complete work orders", "Maintenance"),
    _p(Perm.WORK_ORDER_APPROVE, "Approve work order spend", "Maintenance", sensitive=True),
    _p(Perm.INSPECTION_READ, "View inspections", "Maintenance"),
    _p(Perm.INSPECTION_MANAGE, "Schedule inspections", "Maintenance"),
    _p(Perm.INSPECTION_PERFORM, "Perform inspections", "Maintenance"),
    _p(Perm.PM_MANAGE, "Manage preventive maintenance", "Maintenance"),
    # vendors
    _p(Perm.VENDOR_READ, "View vendors", "Vendors"),
    _p(Perm.VENDOR_MANAGE, "Manage vendors", "Vendors", sensitive=True),
    _p(Perm.VENDOR_COMPLIANCE_MANAGE, "Manage vendor compliance", "Vendors"),
    # documents
    _p(Perm.DOCUMENT_READ, "View documents", "Documents"),
    _p(Perm.DOCUMENT_UPLOAD, "Upload documents", "Documents"),
    _p(Perm.DOCUMENT_DELETE, "Delete documents", "Documents", sensitive=True),
    _p(Perm.DOCUMENT_SHARE, "Share documents externally", "Documents", sensitive=True),
    # assets
    _p(Perm.ASSET_READ, "View assets", "Assets"),
    _p(Perm.ASSET_MANAGE, "Manage assets", "Assets"),
    # reporting and automation
    _p(Perm.REPORT_READ, "View reports", "Reporting"),
    _p(Perm.REPORT_RUN, "Run reports", "Reporting"),
    _p(Perm.REPORT_SCHEDULE, "Schedule reports", "Reporting"),
    _p(Perm.DATA_EXPORT, "Export data", "Reporting", sensitive=True),
    _p(Perm.AUTOMATION_READ, "View automations", "Automation"),
    _p(Perm.AUTOMATION_MANAGE, "Edit automations", "Automation"),
    _p(
        Perm.AUTOMATION_PUBLISH,
        "Publish automations live",
        "Automation",
        "Promotes a rule out of dry run so it can act on production data.",
        sensitive=True,
    ),
    _p(Perm.APPROVAL_DECIDE, "Decide approvals", "Automation", sensitive=True),
    # integration
    _p(Perm.INTEGRATION_READ, "View integrations", "Integration"),
    _p(Perm.INTEGRATION_MANAGE, "Manage integrations", "Integration", sensitive=True),
    _p(Perm.WEBHOOK_MANAGE, "Manage webhooks", "Integration", sensitive=True),
    _p(Perm.IMPORT_RUN, "Run data imports", "Integration", sensitive=True),
)


def all_permission_codes() -> set[str]:
    return {definition.code for definition in PERMISSION_CATALOG}


def sensitive_permission_codes() -> set[str]:
    return {definition.code for definition in PERMISSION_CATALOG if definition.sensitive}


@dataclass(frozen=True)
class RoleDef:
    code: str
    name: str
    description: str
    permissions: frozenset[str]
    requires_mfa: bool = False
    default_for: UserType | None = None


_READ_ONLY_OPERATIONS = frozenset(
    {
        Perm.ORG_READ,
        Perm.PORTFOLIO_READ,
        Perm.PROPERTY_READ,
        Perm.UNIT_READ,
        Perm.RESIDENT_READ,
        Perm.LEASE_READ,
        Perm.LEAD_READ,
        Perm.APPLICATION_READ,
        Perm.REQUEST_READ,
        Perm.WORK_ORDER_READ,
        Perm.INSPECTION_READ,
        Perm.VENDOR_READ,
        Perm.DOCUMENT_READ,
        Perm.ASSET_READ,
        Perm.REPORT_READ,
        Perm.LEDGER_READ,
        Perm.INVOICE_READ,
        Perm.PAYMENT_READ,
        Perm.BILL_READ,
        Perm.OWNER_READ,
    }
)

_LEASING_OPERATIONS = frozenset(
    {
        Perm.LEAD_READ,
        Perm.LEAD_MANAGE,
        Perm.APPLICATION_READ,
        Perm.APPLICATION_MANAGE,
        Perm.SCREENING_READ,
        Perm.SCREENING_ORDER,
        Perm.LEASE_READ,
        Perm.LEASE_CREATE,
        Perm.LEASE_UPDATE,
        Perm.LEASE_RENEW,
        Perm.RESIDENT_READ,
        Perm.RESIDENT_MANAGE,
        Perm.MESSAGE_READ,
        Perm.MESSAGE_SEND,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
        Perm.PROPERTY_READ,
        Perm.UNIT_READ,
        Perm.UNIT_MANAGE,
        Perm.REPORT_READ,
        Perm.REPORT_RUN,
    }
)

_MAINTENANCE_OPERATIONS = frozenset(
    {
        Perm.REQUEST_READ,
        Perm.REQUEST_CREATE,
        Perm.REQUEST_TRIAGE,
        Perm.WORK_ORDER_READ,
        Perm.WORK_ORDER_CREATE,
        Perm.WORK_ORDER_UPDATE,
        Perm.WORK_ORDER_ASSIGN,
        Perm.WORK_ORDER_COMPLETE,
        Perm.INSPECTION_READ,
        Perm.INSPECTION_MANAGE,
        Perm.INSPECTION_PERFORM,
        Perm.PM_MANAGE,
        Perm.VENDOR_READ,
        Perm.VENDOR_COMPLIANCE_MANAGE,
        Perm.ASSET_READ,
        Perm.ASSET_MANAGE,
        Perm.PROPERTY_READ,
        Perm.UNIT_READ,
        Perm.RESIDENT_READ,
        Perm.MESSAGE_READ,
        Perm.MESSAGE_SEND,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
        Perm.REPORT_READ,
    }
)

_ACCOUNTING_OPERATIONS = frozenset(
    {
        Perm.LEDGER_READ,
        Perm.LEDGER_POST,
        Perm.INVOICE_READ,
        Perm.INVOICE_MANAGE,
        Perm.PAYMENT_READ,
        Perm.PAYMENT_RECORD,
        Perm.DEPOSIT_READ,
        Perm.DEPOSIT_COLLECT,
        Perm.BILL_READ,
        Perm.BILL_MANAGE,
        Perm.BANK_ACCOUNT_READ,
        Perm.RECONCILIATION_READ,
        Perm.RECONCILIATION_MANAGE,
        Perm.OWNER_READ,
        Perm.OWNER_STATEMENT_READ,
        Perm.PROPERTY_READ,
        Perm.UNIT_READ,
        Perm.LEASE_READ,
        Perm.RESIDENT_READ,
        Perm.VENDOR_READ,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
        Perm.REPORT_READ,
        Perm.REPORT_RUN,
    }
)

#: Everything a controller adds on top of an accountant: the authority to
#: approve, disburse, and close. Split deliberately - the person who enters a
#: bill must not be the person who pays it.
_CONTROLLER_OPERATIONS = _ACCOUNTING_OPERATIONS | frozenset(
    {
        Perm.LEDGER_REVERSE,
        Perm.ACCOUNT_MANAGE,
        Perm.INVOICE_VOID,
        Perm.PAYMENT_REFUND,
        Perm.DEPOSIT_RELEASE,
        Perm.BILL_APPROVE,
        Perm.BILL_PAY,
        Perm.BANK_ACCOUNT_MANAGE,
        Perm.PERIOD_CLOSE,
        Perm.DISTRIBUTION_MANAGE,
        Perm.DISTRIBUTION_APPROVE,
        Perm.APPROVAL_DECIDE,
        Perm.AUDIT_READ,
        Perm.REPORT_SCHEDULE,
        Perm.DATA_EXPORT,
    }
)

_PORTAL_RESIDENT = frozenset(
    {
        Perm.LEASE_READ,
        Perm.INVOICE_READ,
        Perm.PAYMENT_READ,
        Perm.PAYMENT_RECORD,
        # Read only. A resident may see what is held for them; whether it comes
        # back is decided at the disposition, by somebody else.
        Perm.DEPOSIT_READ,
        Perm.REQUEST_READ,
        Perm.REQUEST_CREATE,
        Perm.WORK_ORDER_READ,
        Perm.MESSAGE_READ,
        Perm.MESSAGE_SEND,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
    }
)

_PORTAL_OWNER = frozenset(
    {
        Perm.PROPERTY_READ,
        Perm.UNIT_READ,
        Perm.OWNER_STATEMENT_READ,
        Perm.LEDGER_READ,
        Perm.INVOICE_READ,
        Perm.BILL_READ,
        Perm.WORK_ORDER_READ,
        Perm.INSPECTION_READ,
        Perm.DOCUMENT_READ,
        Perm.REPORT_READ,
        Perm.REPORT_RUN,
        Perm.MESSAGE_READ,
        Perm.MESSAGE_SEND,
        Perm.ASSET_READ,
    }
)

_PORTAL_VENDOR = frozenset(
    {
        Perm.WORK_ORDER_READ,
        Perm.WORK_ORDER_UPDATE,
        Perm.WORK_ORDER_COMPLETE,
        Perm.INSPECTION_READ,
        Perm.INSPECTION_PERFORM,
        Perm.DOCUMENT_READ,
        Perm.DOCUMENT_UPLOAD,
        Perm.MESSAGE_READ,
        Perm.MESSAGE_SEND,
        Perm.BILL_READ,
    }
)

SYSTEM_ROLES: Final[tuple[RoleDef, ...]] = (
    RoleDef(
        code="org_admin",
        name="Organization administrator",
        description="Full authority within the organization, including identity and settings.",
        permissions=frozenset(all_permission_codes()) - {Perm.USER_IMPERSONATE, Perm.PERIOD_REOPEN},
        requires_mfa=True,
    ),
    RoleDef(
        code="controller",
        name="Controller",
        description="Owns the books: approvals, disbursements, reconciliation, and close.",
        permissions=_CONTROLLER_OPERATIONS,
        requires_mfa=True,
    ),
    RoleDef(
        code="accountant",
        name="Accountant",
        description="Day-to-day bookkeeping without approval or disbursement authority.",
        permissions=_ACCOUNTING_OPERATIONS,
        requires_mfa=True,
    ),
    RoleDef(
        code="property_manager",
        name="Property manager",
        description="Operational authority over assigned properties.",
        permissions=_LEASING_OPERATIONS
        | _MAINTENANCE_OPERATIONS
        | {
            Perm.APPLICATION_DECIDE,
            Perm.LEASE_TERMINATE,
            Perm.NOTICE_ISSUE,
            Perm.INVOICE_READ,
            Perm.PAYMENT_READ,
            Perm.BILL_READ,
            Perm.BILL_MANAGE,
            Perm.OWNER_READ,
            Perm.WORK_ORDER_APPROVE,
            Perm.VENDOR_READ,
            Perm.AUDIT_READ,
            Perm.PORTFOLIO_READ,
        },
    ),
    RoleDef(
        code="leasing_agent",
        name="Leasing agent",
        description="Lead-to-lease pipeline, without final approval authority.",
        permissions=_LEASING_OPERATIONS,
        default_for=None,
    ),
    RoleDef(
        code="maintenance_dispatcher",
        name="Maintenance dispatcher",
        description="Triage, dispatch, and vendor coordination.",
        permissions=_MAINTENANCE_OPERATIONS,
    ),
    RoleDef(
        code="technician",
        name="Maintenance technician",
        description="Executes assigned work; sees only what is assigned.",
        permissions=frozenset(
            {
                Perm.WORK_ORDER_READ,
                Perm.WORK_ORDER_UPDATE,
                Perm.WORK_ORDER_COMPLETE,
                Perm.INSPECTION_READ,
                Perm.INSPECTION_PERFORM,
                Perm.REQUEST_READ,
                Perm.ASSET_READ,
                Perm.DOCUMENT_READ,
                Perm.DOCUMENT_UPLOAD,
                Perm.PROPERTY_READ,
                Perm.UNIT_READ,
                Perm.MESSAGE_READ,
                Perm.MESSAGE_SEND,
            }
        ),
    ),
    RoleDef(
        code="auditor",
        name="Auditor",
        description="Read-only access across operations and the audit trail.",
        permissions=_READ_ONLY_OPERATIONS
        | {Perm.AUDIT_READ, Perm.AUDIT_EXPORT, Perm.REPORT_RUN, Perm.RECONCILIATION_READ},
        requires_mfa=True,
    ),
    RoleDef(
        code="resident",
        name="Resident",
        description="Self-service portal access, scoped to their own tenancy.",
        permissions=_PORTAL_RESIDENT,
        default_for=UserType.RESIDENT,
    ),
    RoleDef(
        code="owner",
        name="Owner",
        description="Owner portal access, scoped to owned properties.",
        permissions=_PORTAL_OWNER,
        default_for=UserType.OWNER,
    ),
    RoleDef(
        code="vendor",
        name="Vendor",
        description="Vendor portal access, scoped to assigned work.",
        permissions=_PORTAL_VENDOR,
        default_for=UserType.VENDOR,
    ),
)


def permissions_for_role(code: str) -> frozenset[str]:
    for role in SYSTEM_ROLES:
        if role.code == code:
            return role.permissions
    raise KeyError(f"Unknown system role: {code}")


def validate_catalog() -> None:
    """Every role references real permissions, and no code is defined twice.

    Called by the test suite: a typo in a role's permission set would otherwise
    silently grant nothing, which reads as "correctly denied" in every test that
    is not looking for it.
    """
    codes = [definition.code for definition in PERMISSION_CATALOG]
    duplicates = {code for code in codes if codes.count(code) > 1}
    if duplicates:
        raise AssertionError(f"Duplicate permission codes: {sorted(duplicates)}")

    known = set(codes)
    declared = {
        value
        for name, value in vars(Perm).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    missing = declared - known
    if missing:
        raise AssertionError(
            f"Permissions declared on Perm but absent from catalog: {sorted(missing)}"
        )

    for role in SYSTEM_ROLES:
        unknown = role.permissions - known
        if unknown:
            raise AssertionError(
                f"Role {role.code} references unknown permissions: {sorted(unknown)}"
            )
