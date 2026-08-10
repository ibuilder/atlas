"""Cross-cutting service primitives: transactions, locking, numbering.

Deliberately dependency-free with respect to the rest of ``app.services``.
Everything here is imported *by* services, so importing a service from here
would close a cycle - which is why bulk import lives in ``app.services.imports``
rather than alongside these.

SPDX-License-Identifier: MIT
"""

from app.services.common.numbering import next_number, peek_number
from app.services.common.unit_of_work import lock_row, transaction

__all__ = ["lock_row", "next_number", "peek_number", "transaction"]
