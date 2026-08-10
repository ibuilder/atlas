"""The default chart of accounts.

A property-management-shaped starting chart. Codes are stable identifiers that
posting rules reference by name, so a tenant may rename an account but must not
renumber a system one.

Security deposits are a liability, not income - money held on behalf of someone
else. Getting that wrong overstates revenue and, in trust-accounting
jurisdictions, is a licensing problem rather than a bookkeeping one.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.accounting import Account, AccountType, NormalBalance

__all__ = ["ACCOUNT_CODES", "DEFAULT_CHART", "AccountCode", "seed_chart_of_accounts"]


class AccountCode:
    """System account codes referenced by posting rules."""

    CASH_OPERATING = "1000"
    CASH_TRUST = "1010"
    ACCOUNTS_RECEIVABLE = "1100"
    PREPAID_EXPENSES = "1200"
    BUILDINGS = "1500"
    ACCUMULATED_DEPRECIATION = "1590"

    ACCOUNTS_PAYABLE = "2000"
    SECURITY_DEPOSITS_HELD = "2100"
    PREPAID_RENT = "2200"
    OWNER_DISTRIBUTIONS_PAYABLE = "2300"

    OWNER_EQUITY = "3000"
    RETAINED_EARNINGS = "3900"

    RENTAL_INCOME = "4000"
    LATE_FEE_INCOME = "4100"
    OTHER_INCOME = "4200"
    APPLICATION_FEE_INCOME = "4300"

    REPAIRS_MAINTENANCE = "5000"
    UTILITIES = "5100"
    INSURANCE = "5200"
    PROPERTY_TAXES = "5300"
    MANAGEMENT_FEES = "5400"
    TURNOVER_COSTS = "5500"
    LEGAL_PROFESSIONAL = "5600"
    BAD_DEBT = "5700"
    OTHER_EXPENSE = "5900"


#: ``(code, name, type, normal balance, is_control, is_bank, is_trust)``
DEFAULT_CHART: tuple[tuple[str, str, AccountType, NormalBalance, bool, bool, bool], ...] = (
    (
        AccountCode.CASH_OPERATING,
        "Cash - Operating",
        AccountType.ASSET,
        NormalBalance.DEBIT,
        False,
        True,
        False,
    ),
    (
        AccountCode.CASH_TRUST,
        "Cash - Trust",
        AccountType.ASSET,
        NormalBalance.DEBIT,
        False,
        True,
        True,
    ),
    (
        AccountCode.ACCOUNTS_RECEIVABLE,
        "Accounts Receivable",
        AccountType.ASSET,
        NormalBalance.DEBIT,
        True,
        False,
        False,
    ),
    (
        AccountCode.PREPAID_EXPENSES,
        "Prepaid Expenses",
        AccountType.ASSET,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.BUILDINGS,
        "Buildings and Improvements",
        AccountType.ASSET,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.ACCUMULATED_DEPRECIATION,
        "Accumulated Depreciation",
        AccountType.ASSET,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.ACCOUNTS_PAYABLE,
        "Accounts Payable",
        AccountType.LIABILITY,
        NormalBalance.CREDIT,
        True,
        False,
        False,
    ),
    (
        AccountCode.SECURITY_DEPOSITS_HELD,
        "Security Deposits Held",
        AccountType.LIABILITY,
        NormalBalance.CREDIT,
        True,
        False,
        True,
    ),
    (
        AccountCode.PREPAID_RENT,
        "Prepaid Rent",
        AccountType.LIABILITY,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.OWNER_DISTRIBUTIONS_PAYABLE,
        "Owner Distributions Payable",
        AccountType.LIABILITY,
        NormalBalance.CREDIT,
        True,
        False,
        False,
    ),
    (
        AccountCode.OWNER_EQUITY,
        "Owner Equity",
        AccountType.EQUITY,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.RETAINED_EARNINGS,
        "Retained Earnings",
        AccountType.EQUITY,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.RENTAL_INCOME,
        "Rental Income",
        AccountType.REVENUE,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.LATE_FEE_INCOME,
        "Late Fee Income",
        AccountType.REVENUE,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.OTHER_INCOME,
        "Other Income",
        AccountType.REVENUE,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.APPLICATION_FEE_INCOME,
        "Application Fee Income",
        AccountType.REVENUE,
        NormalBalance.CREDIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.REPAIRS_MAINTENANCE,
        "Repairs and Maintenance",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.UTILITIES,
        "Utilities",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.INSURANCE,
        "Insurance",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.PROPERTY_TAXES,
        "Property Taxes",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.MANAGEMENT_FEES,
        "Management Fees",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.TURNOVER_COSTS,
        "Turnover Costs",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.LEGAL_PROFESSIONAL,
        "Legal and Professional",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.BAD_DEBT,
        "Bad Debt Expense",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
    (
        AccountCode.OTHER_EXPENSE,
        "Other Expense",
        AccountType.EXPENSE,
        NormalBalance.DEBIT,
        False,
        False,
        False,
    ),
)

ACCOUNT_CODES = tuple(row[0] for row in DEFAULT_CHART)


def seed_chart_of_accounts(session: Session, org_id: str) -> dict[str, Account]:
    """Create any missing system accounts. Idempotent."""
    existing = {
        account.code: account
        for account in session.execute(select(Account).where(Account.org_id == org_id)).scalars()
    }

    for code, name, account_type, normal, is_control, is_bank, is_trust in DEFAULT_CHART:
        if code in existing:
            continue
        account = Account(
            org_id=org_id,
            code=code,
            name=name,
            account_type=account_type,
            normal_balance=normal,
            is_control=is_control,
            is_bank=is_bank,
            is_trust=is_trust,
            is_system=True,
            is_active=True,
        )
        session.add(account)
        existing[code] = account

    session.flush()
    return existing


def account_by_code(session: Session, org_id: str, code: str) -> Account:
    """Resolve a system account, or fail loudly.

    Posting rules reference accounts by code. A missing one means the chart was
    not provisioned, which must surface immediately rather than as a confusing
    foreign key error three frames deeper.
    """
    from app.errors import NotFound

    account = session.execute(
        select(Account).where(Account.org_id == org_id, Account.code == code)
    ).scalar_one_or_none()
    if account is None:
        raise NotFound(f"System account {code} is not provisioned for this organization.")
    return account


__all__ += ["account_by_code"]
