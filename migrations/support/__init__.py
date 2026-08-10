"""Helpers shared by migrations.

Deliberately thin. A migration must keep meaning what it meant when it was
written, so anything whose *semantics* could sensibly change is inlined in the
migration that uses it rather than imported from here.

SPDX-License-Identifier: MIT
"""
