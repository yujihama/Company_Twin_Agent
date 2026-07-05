"""World-natural display-name registry for seats.

A blind SME review (data/design/MASTER_DESIGN.md §12 "SME盲検") flagged
symbolic seat ids appearing directly in rendered records/prose ("emp-B",
"emp-M様", and even a broken concatenation "emp-Wemp-H") as an artificial
marker -- no real employee record refers to a colleague as "emp-B".

This module maps each seat_id to a deterministic, world-natural display name
(department + Japanese surname), e.g. emp-A -> "営業部 佐藤". It is used ONLY
for RENDERING text that a seat/customer will read (chat "from" lines, inbox
summaries, addressing in prose). It must never replace seat_id in anything the
kernel/recorder/oracles key on:

- recorder.record_chat/record_inbox/append_ledger continue to store the raw
  seat_id (experimenter-plane truth; L0/L1 oracles and bucket signatures key
  on seat_id, per MASTER_DESIGN §7.3/§10).
- kernel permission checks, basis records, and world_config bindings are
  untouched.

Names are intentionally plain, deterministic, and collision-free across the
DFH pack v0 roster (data/design/MASTER_DESIGN.md §7.1): emp-A/B/F/G sales,
emp-C application, emp-M manager, emp-Q second_line, plus the world-internal
audit actor.
"""
from __future__ import annotations

# seat_id -> (department, surname). Both are ordinary Japanese business-register
# terms; the pairing is arbitrary but fixed (deterministic per seat, never
# randomized per run) so the same seat always renders the same way across a
# run bundle and across reruns.
_SEAT_DISPLAY_REGISTRY: dict[str, tuple[str, str]] = {
    "emp-A": ("営業部", "佐藤"),
    "emp-B": ("営業部", "鈴木"),
    "emp-F": ("加盟店営業部", "高橋"),
    "emp-G": ("口座営業部", "田中"),
    "emp-C": ("申込管理部", "伊藤"),
    "emp-M": ("営業管理部", "渡辺"),
    "emp-Q": ("品質管理部", "山本"),
    "audit-in-world": ("監査部", "中村"),
}

_FALLBACK_DEPARTMENT = "本部"
_FALLBACK_SURNAMES = ("小林", "加藤", "吉田", "山口", "松本", "井上", "木村", "林", "斎藤", "清水")


def display_name_for_seat(seat_id: str) -> str:
    """Return a deterministic world-natural display name for a seat_id.

    e.g. "emp-A" -> "営業部 佐藤". Unknown seat_ids (outside the fixed DFH pack
    v0 roster) still get a deterministic name derived from a stable hash of
    the seat_id, so rendering never falls back to leaking the raw seat_id.
    """
    entry = _SEAT_DISPLAY_REGISTRY.get(seat_id)
    if entry is not None:
        department, surname = entry
        return f"{department} {surname}"
    # Deterministic fallback for any seat_id not in the fixed roster (e.g. a
    # future seat added to a world pack): stable per seat_id, never random.
    index = sum(ord(char) for char in seat_id) % len(_FALLBACK_SURNAMES)
    return f"{_FALLBACK_DEPARTMENT} {_FALLBACK_SURNAMES[index]}"


def render_seat_reference(seat_id: str, *, honorific: str = "") -> str:
    """Render a seat_id as a natural in-prose reference, e.g. for addressing
    a colleague: render_seat_reference("emp-M", honorific="さん") ->
    "営業管理部の渡辺さん"."""
    name = display_name_for_seat(seat_id)
    department, surname = name.rsplit(" ", 1)
    return f"{department}の{surname}{honorific}"


def display_names_for_seats(seat_ids: list[str]) -> dict[str, str]:
    """Bulk helper: {seat_id: display_name} for a list of seat_ids."""
    return {seat_id: display_name_for_seat(seat_id) for seat_id in seat_ids}
