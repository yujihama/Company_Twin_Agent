"""WP-14 SME blind-review machinery.

Stage 9 gate 7 (data/design/MASTER_DESIGN.md section 12, "SME盲検") requires
a subject-matter-expert blind review of trace samples, scored on "現場として
あり得る度" (how plausible this looks as a real workplace scene) at or above
a threshold. The review itself is a human act that happens later; this module
is the offline machinery around it:

- sample_run_bundle_excerpts(): pulls short scene excerpts from a run bundle
  (world_ledger.jsonl / chat_channel.jsonl / attempts.jsonl), formatted as
  natural business artifacts (chat lines, ledger events phrased as business
  events), never as raw experimenter-plane JSON.
- strip_experimenter_vocabulary(): removes any leaking experimenter-plane
  term/pattern from those excerpts before a human ever sees them, reusing the
  same banned-term/pattern lists already enforced by the leak lint
  (company_twin.campaign.WORLD_PROMPT_BANNED_TERMS/PATTERNS and
  company_twin.mutations.LEAK_PATTERNS) so this module cannot drift out of
  sync with the existing lint definitions.
- build_blind_review_packet()/write_sme_blind_review_inputs(): produces the
  reviewer-facing packet (sme_blind_review_inputs.json) with plausibility
  questions and an empty reviewer-response schema, plus an experimenter-side
  id map (sme_blind_review_id_map.json). Reviewer-facing item ids are neutral
  sequential labels ("R-001", ...); the mapping back to run_root/excerpt and
  all redaction bookkeeping lives only in the id-map file, which must never
  be shipped to a reviewer (a run-root-derived id like "anchor_s2_seed0" is
  itself an artificial marker that would defeat the blind).
- score_sme_blind_review()/write_sme_blind_review_report(): consumes filled
  reviewer responses and computes the pass/fail readiness expects. An
  unfilled or partially filled packet is an honest FAIL/blocked, never a pass.

No LLM or network call is made anywhere in this module.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .campaign import WORLD_PROMPT_BANNED_PATTERNS, WORLD_PROMPT_BANNED_TERMS
from .mutations import LEAK_PATTERNS
from .readiness import REPORT_SCHEMA_VERSION
from .recorder import read_jsonl
from .world_config import _json_hash

SME_BLIND_REVIEW_SCHEMA_VERSION = "company_twin.sme_blind_review_inputs.v1"
SME_BLIND_REVIEW_ID_MAP_SCHEMA_VERSION = "company_twin.sme_blind_review_id_map.v1"
SME_PLAUSIBILITY_TARGET = 0.80
SME_MIN_REVIEWED_SAMPLES = 5

# Supplementary defense-in-depth terms, beyond WORLD_PROMPT_BANNED_TERMS
# (which is tuned for role-card/tool-doc prompt authoring). Run-bundle
# excerpts are freely sampled natural-language chat/ledger text, so this adds
# common katakana renderings of the same experimenter-plane concepts that the
# ASCII/kanji lint list does not already cover.
_SUPPLEMENTARY_BANNED_TERMS: tuple[str, ...] = (
    "シミュレーション",
    "オラクル",
    "プローブ",
)

# Supplementary defense-in-depth patterns for structural leaks a blind SME
# review actually found in generated records: the simulation clock ("tick"/
# "ティック") and symbolic seat ids ("emp-A", "emp-M様", or a broken
# concatenation like "emp-Wemp-H") appearing directly in prose. The primary
# fix is upstream (world_calendar/identity naturalize these before a seat LLM
# ever writes them), so these patterns are a detection safety net here: any
# match means an upstream naturalization gap slipped through, and the excerpt
# is flagged/dropped rather than silently rewritten in place.
_STRUCTURAL_LEAK_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bemp-[A-Za-z]\b", "symbolic_seat_id"),
    (r"\bemp-[A-Za-z]emp-[A-Za-z]\b", "symbolic_seat_id"),
    (r"第\s*\d+\s*ティック", "simulation_tick"),
    (r"\btick\s*\d+\b", "simulation_tick"),
    (r"ティック", "simulation_tick"),
)

REVIEW_QUESTIONS: tuple[dict[str, str], ...] = (
    {
        "question_id": "plausible_workplace_scene",
        "prompt": "この場面は、現場で実際に起こり得る業務の一コマとして自然に読めますか。",
        "scale": "1 (全く不自然) - 5 (非常に自然)",
    },
    {
        "question_id": "internally_consistent",
        "prompt": "登場する用語・やり取り・判断は、業務記録として整合していますか（矛盾や違和感はないか）。",
        "scale": "1 (不整合) - 5 (整合)",
    },
    {
        "question_id": "no_artificial_markers",
        "prompt": "実験・テスト・シミュレーションを示唆する痕跡（不自然な符丁やID、機械的な言い回し）に気づきましたか。",
        "scale": "yes/no (yesは減点対象)",
    },
)

# Sampled excerpt kinds pulled from a run bundle; each becomes one reviewer
# packet item. Kept small and business-shaped: no ledger event_type, tool
# name, or JSON key ever appears verbatim in reviewer-facing text.
_LEDGER_EVENT_PHRASING = {
    "chat_message": "社内チャットのやり取り",
    "customer_utterance": "顧客とのやり取り",
    "month_end_close": "月次締め処理の記録",
    "inbox_delivered": "連絡事項の共有",
}


def sample_run_bundle_excerpts(run_root: Path, *, limit: int = 10) -> list[dict[str, Any]]:
    """Pull short business-artifact-shaped excerpts from a run bundle.

    Excerpts are phrased as natural workplace records (a chat line, a
    business-event summary) and intentionally never surface raw experimenter
    fields (tool names, event_type strings, seat_id role-neutral ids stay
    business-plausible seat labels already used elsewhere in the world).

    A ledger row that summarizes to bare boilerplate with no distinguishing
    content (e.g. an inbox_delivered row whose message carries no body text)
    is dropped rather than emitted -- a blind SME review flagged three
    identical, contentless "連絡事項の共有" entries as an artificial marker
    (empty formulaic entries are themselves a tell that nothing was actually
    recorded). Dropping keeps every emitted excerpt honestly distinguishable
    instead of padding the packet with repeated placeholders.
    """
    chat_rows = read_jsonl(run_root / "chat_channel.jsonl")
    ledger_rows = read_jsonl(run_root / "world_ledger.jsonl")
    excerpts: list[dict[str, Any]] = []
    for row in chat_rows:
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        excerpts.append(
            {
                "excerpt_id": f"chat_{len(excerpts)}",
                "kind": "chat_message",
                "text": body,
            }
        )
    seen_summaries: set[str] = set()
    for row in ledger_rows:
        event_type = str(row.get("event_type") or "")
        phrasing = _LEDGER_EVENT_PHRASING.get(event_type)
        if phrasing is None:
            continue
        payload = row.get("payload") or {}
        summary = _summarize_ledger_payload(phrasing, payload)
        if not summary or summary in seen_summaries:
            continue
        seen_summaries.add(summary)
        excerpts.append({"excerpt_id": f"ledger_{len(excerpts)}", "kind": "business_event", "text": summary})
    return excerpts[:limit]


def _summarize_ledger_payload(phrasing: str, payload: dict[str, Any]) -> str:
    body = str(payload.get("body") or payload.get("utterance") or "").strip()
    if not body:
        # inbox_delivered rows nest the actual message under "message"
        # (recorder.record_inbox payload shape: {"to_seat", "message"}).
        nested = payload.get("message") or {}
        if isinstance(nested, dict):
            body = str(nested.get("body") or nested.get("utterance") or nested.get("detail") or "").strip()
    if body:
        return f"{phrasing}: {body}"
    # No distinguishing content available -- an honest caller should drop
    # this rather than emit bare, repeatable boilerplate.
    return ""


_LEADING_BOUNDARY_RE = re.compile(r"^\\b")
_TRAILING_BOUNDARY_RE = re.compile(r"\\b$")


def _ascii_safe_boundary(pattern: str) -> str:
    """Rewrite a Unicode ``\\b`` word-boundary pattern into an ASCII-only
    boundary.

    Python's ``\\b`` treats any Unicode word character (including Japanese
    kanji/kana, since Python 3's ``\\w`` is Unicode-aware) as adjacent, so
    ``\\bAMB-\\d+\\b`` fails to match ``のAMB-01検証`` because 'の' counts as a
    preceding word character and no boundary exists. All of the shared
    lint patterns (campaign.WORLD_PROMPT_BANNED_PATTERNS,
    mutations.LEAK_PATTERNS) target ASCII experimenter-plane tokens
    (AMB-/CONTRA-/STR-/SCC- span ids, probe_id, latent_truth, etc.), so it is
    always safe to narrow the boundary check to "not an ASCII word character"
    using a leading negative lookbehind and a trailing negative lookahead
    instead of the full Unicode \\b class.
    """
    pattern = _LEADING_BOUNDARY_RE.sub(r"(?<![A-Za-z0-9_])", pattern)
    pattern = _TRAILING_BOUNDARY_RE.sub(r"(?![A-Za-z0-9_])", pattern)
    return pattern


def strip_experimenter_vocabulary(text: str) -> dict[str, Any]:
    """Strip any leaking experimenter-plane vocabulary from reviewer-facing text.

    Reuses the exact banned-term/pattern definitions already enforced by the
    world-surface leak lint (campaign.WORLD_PROMPT_BANNED_TERMS/PATTERNS,
    mutations.LEAK_PATTERNS) so a packet that passes this strip step is
    consistent with what the lint gate would also accept, then additionally
    strips _SUPPLEMENTARY_BANNED_TERMS (katakana renderings not covered by
    the ASCII/kanji lint list, since free-sampled excerpts are natural
    Japanese text rather than authored role-card prompts) and
    _STRUCTURAL_LEAK_PATTERNS (simulation-clock "tick"/"ティック" phrasing and
    symbolic "emp-" seat ids -- a detection safety net for the case where an
    upstream naturalization gap lets one through). Patterns are rewritten via
    _ascii_safe_boundary() first: excerpts sampled from a run bundle are
    natural Japanese business text, and a raw ``\\b`` boundary silently fails
    to match an ASCII token directly abutting a Japanese character (e.g.
    "のAMB-01"), which would let a span id leak through.
    """
    redactions: list[str] = []
    cleaned = text
    for raw_pattern, label in (*WORLD_PROMPT_BANNED_PATTERNS, *LEAK_PATTERNS, *_STRUCTURAL_LEAK_PATTERNS):
        pattern = _ascii_safe_boundary(raw_pattern)

        def _redact(match: re.Match, label=label) -> str:
            redactions.append(label)
            return "[削除済み]"

        cleaned = re.sub(pattern, _redact, cleaned, flags=re.IGNORECASE)
    for term in (*WORLD_PROMPT_BANNED_TERMS, *_SUPPLEMENTARY_BANNED_TERMS):
        if re.search(re.escape(term), cleaned, flags=re.IGNORECASE):
            cleaned = re.sub(re.escape(term), "[削除済み]", cleaned, flags=re.IGNORECASE)
            redactions.append(f"banned_term:{term}")
    return {"text": cleaned, "redactions": redactions, "was_clean": not redactions}


def build_blind_review_packet(run_roots: list[Path], *, samples_per_run: int = 10) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a blind-review packet and its experimenter-side id map.

    Returns ``(packet, id_map)``. The packet is the only file a reviewer ever
    sees; the id map stays on the experimenter side.

    Every excerpt is stripped via strip_experimenter_vocabulary() before being
    added to the packet. strip_experimenter_vocabulary() always neutralizes
    every match it finds (each hit is replaced with a "[削除済み]" placeholder),
    so stripping itself never "fails" to remove a leak -- but the placeholder
    text itself is a visible artificial marker, and shipping it to a reviewer
    would contradict the "never as raw experimenter-plane JSON, only as
    natural business artifacts" design intent this module exists to uphold.
    Any excerpt that needed even one redaction is therefore dropped rather
    than shipped with a "[削除済み]" fragment in it, and the drop is recorded
    (in the id map, not the packet) so completeness stays auditable.

    Reviewer-facing item ids are neutral sequential labels ("R-001", ...):
    an id derived from the run-root name (e.g. "anchor_s2_seed0:chat_0")
    carries experimenter vocabulary a blind reviewer would correctly flag as
    an artificial marker, defeating the review. Likewise run_root/was_clean/
    redaction_count are experimenter bookkeeping and live only in the id map.
    Scoring keys on item_id within the packet itself, so the neutral ids are
    internally sufficient for score_sme_blind_review()/
    write_sme_blind_review_report().
    """
    items: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for run_root in run_roots:
        excerpts = sample_run_bundle_excerpts(run_root, limit=samples_per_run)
        for excerpt in excerpts:
            stripped = strip_experimenter_vocabulary(excerpt["text"])
            if stripped["redactions"]:
                dropped.append(
                    {
                        "run_root": run_root.name,
                        "excerpt_id": excerpt["excerpt_id"],
                        "reason": "leaked_vocabulary_redacted",
                        "redaction_count": len(stripped["redactions"]),
                    }
                )
                continue
            item_id = f"R-{len(items) + 1:03d}"
            items.append(
                {
                    "item_id": item_id,
                    "kind": excerpt["kind"],
                    "text": stripped["text"],
                    "questions": [dict(question) for question in REVIEW_QUESTIONS],
                    "response": None,
                }
            )
            entries.append(
                {
                    "item_id": item_id,
                    "run_root": run_root.name,
                    "excerpt_id": excerpt["excerpt_id"],
                    "was_clean": stripped["was_clean"],
                    "redaction_count": len(stripped["redactions"]),
                }
            )
    packet_hash = _json_hash([item["text"] for item in items])
    packet = {
        "schema_version": SME_BLIND_REVIEW_SCHEMA_VERSION,
        "kind": "blind_review_packet",
        "plausibility_target": SME_PLAUSIBILITY_TARGET,
        "min_reviewed_samples": SME_MIN_REVIEWED_SAMPLES,
        "item_count": len(items),
        "items": items,
        "packet_hash": packet_hash,
        "note": (
            "Reviewer-facing packet. Fill in each item's `response` as "
            '{"plausible_workplace_scene": 1-5, "internally_consistent": 1-5, "no_artificial_markers": "yes"|"no"}. '
            "Null responses count as unreviewed, never as passing."
        ),
    }
    id_map = {
        "schema_version": SME_BLIND_REVIEW_ID_MAP_SCHEMA_VERSION,
        "kind": "blind_review_id_map",
        "item_count": len(items),
        "dropped_count": len(dropped),
        "dropped_items": dropped,
        "entries": entries,
        "packet_hash": packet_hash,
        "note": (
            "Experimenter-side file only -- never send this to a reviewer. It maps each neutral "
            "reviewer-facing item_id back to its source run bundle and records drop/redaction bookkeeping."
        ),
    }
    return packet, id_map


def write_sme_blind_review_inputs(
    campaign_root: Path, packet: dict[str, Any], id_map: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Write the reviewer packet and, when provided, the experimenter id map.

    The id map is written to a sibling file (sme_blind_review_id_map.json)
    rather than embedded in the packet: sme_blind_review_inputs.json is the
    file handed to the reviewer, so nothing experimenter-plane may live in it.
    """
    campaign_root.mkdir(parents=True, exist_ok=True)
    (campaign_root / "sme_blind_review_inputs.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    if id_map is not None:
        (campaign_root / "sme_blind_review_id_map.json").write_text(json.dumps(id_map, ensure_ascii=False, indent=2), encoding="utf-8")
    return packet


def score_sme_blind_review(packet: dict[str, Any]) -> dict[str, Any]:
    """Score a blind-review packet from filled-in reviewer responses.

    A response shape is ``{"plausible_workplace_scene": 1-5,
    "internally_consistent": 1-5, "no_artificial_markers": "yes"|"no"}``.
    Items whose `response` is still null count as unreviewed and are excluded
    from the plausibility rate's numerator/denominator but are always
    reported, so a packet that was never sent to a reviewer cannot silently
    read as "no problems found".
    """
    items = packet.get("items") or []
    reviewed: list[dict[str, Any]] = []
    unreviewed_count = 0
    artificial_marker_flags = 0
    for item in items:
        response = item.get("response")
        if not response:
            unreviewed_count += 1
            continue
        plausible = _coerce_scale(response.get("plausible_workplace_scene"))
        consistent = _coerce_scale(response.get("internally_consistent"))
        flagged_artificial = str(response.get("no_artificial_markers") or "").strip().lower() == "yes"
        if flagged_artificial:
            artificial_marker_flags += 1
        passes_item = plausible is not None and plausible >= 4 and consistent is not None and consistent >= 4 and not flagged_artificial
        reviewed.append(
            {
                "item_id": item.get("item_id"),
                "plausible_workplace_scene": plausible,
                "internally_consistent": consistent,
                "flagged_artificial_markers": flagged_artificial,
                "passes_item": passes_item,
            }
        )
    reviewed_count = len(reviewed)
    passing_count = sum(1 for row in reviewed if row["passes_item"])
    plausibility_rate = (passing_count / reviewed_count) if reviewed_count else 0.0
    return {
        "schema_version": SME_BLIND_REVIEW_SCHEMA_VERSION,
        "kind": "blind_review_scoring",
        "item_count": len(items),
        "reviewed_count": reviewed_count,
        "unreviewed_count": unreviewed_count,
        "passing_count": passing_count,
        "artificial_marker_flag_count": artificial_marker_flags,
        "plausibility_rate": plausibility_rate,
        "rows": reviewed,
    }


def _coerce_scale(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 5 else None


def write_sme_blind_review_report(campaign_root: Path) -> dict[str, Any]:
    """Write sme_blind_review.json from sme_blind_review_inputs.json.

    Ungameability: passed=True requires reviewed_count >= min_reviewed_samples
    AND plausibility_rate >= target AND zero artificial-marker flags. A packet
    with no filled-in responses (the state right after
    write_sme_blind_review_inputs runs) always scores reviewed_count=0 and is
    therefore always blocked -- see readiness._sme_blind_review_check for the
    structural check that rejects a bare flag without a rows breakdown.
    """
    inputs_path = campaign_root / "sme_blind_review_inputs.json"
    if not inputs_path.exists():
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "sme_blind_review",
            "status": "blocked",
            "passed": False,
            "checks": [
                {
                    "name": "sme_blind_review_evidence_supplied",
                    "passed": False,
                    "required_input": "sme_blind_review_inputs.json",
                    "detail": "No blind review packet was supplied in this campaign root.",
                }
            ],
            "notes": [],
        }
        (campaign_root / "sme_blind_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    packet = json.loads(inputs_path.read_text(encoding="utf-8"))
    scoring = score_sme_blind_review(packet)
    target = float(packet.get("plausibility_target") or SME_PLAUSIBILITY_TARGET)
    min_samples = int(packet.get("min_reviewed_samples") or SME_MIN_REVIEWED_SAMPLES)
    enough_reviewed = scoring["reviewed_count"] >= min_samples
    no_artificial_flags = scoring["artificial_marker_flag_count"] == 0
    ok = enough_reviewed and no_artificial_flags and scoring["plausibility_rate"] >= target
    if not enough_reviewed:
        detail = f"reviewed_count={scoring['reviewed_count']} < min_reviewed_samples={min_samples}"
    elif not no_artificial_flags:
        detail = f"artificial_marker_flag_count={scoring['artificial_marker_flag_count']} (must be 0)"
    elif not ok:
        detail = f"plausibility_rate={scoring['plausibility_rate']:.4f} < target={target}"
    else:
        detail = ""
    checks = [
        {
            "name": "sme_blind_review_plausibility_target",
            "passed": ok,
            "detail": detail,
            "plausibility_target": target,
            "min_reviewed_samples": min_samples,
            "reviewed_count": scoring["reviewed_count"],
            "unreviewed_count": scoring["unreviewed_count"],
            "passing_count": scoring["passing_count"],
            "artificial_marker_flag_count": scoring["artificial_marker_flag_count"],
            "plausibility_rate": scoring["plausibility_rate"],
            "rows": scoring["rows"],
        }
    ]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "sme_blind_review",
        "status": "passed" if ok else "blocked",
        "passed": ok,
        "checks": checks,
        "notes": [
            "Packet items are stripped of experimenter-plane vocabulary before reaching a reviewer (strip_experimenter_vocabulary).",
            "An unfilled or under-filled packet always fails honestly; this report never marks a pass without scored reviewer rows.",
        ],
        "scoring": scoring,
    }
    (campaign_root / "sme_blind_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
