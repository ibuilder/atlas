<!-- SPDX-License-Identifier: MIT -->

## What changed, and why

<!-- The why matters more. The diff already says what. -->

## Checks

- [ ] `make check` passes, or the equivalent: ruff, mypy, and the suite
- [ ] Tested against PostgreSQL, not only SQLite, if it touches money, tenancy, or migrations
- [ ] `docs/FEATURES.md` still tells the truth about what this changes
- [ ] A migration is included if a model changed, and it downgrades

## Anything a reviewer should look at twice

<!-- A decision you were unsure about is worth naming. So is a shortcut and the
     reason for it. Both are cheaper to discuss now than to find later. -->
