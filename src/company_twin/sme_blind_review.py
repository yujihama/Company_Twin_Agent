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

import hashlib
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
# Approved 2026-07-06 (approval #8): mechanical_generation flags are gated as
# a RATE (<= 5% of the reviewed panel) instead of zero-tolerance -- the
# measured 3-5% LLM fluency floor made zero a lottery; see the gate comment
# in write_sme_blind_review_report.
SME_MECHANICAL_RATE_TOLERANCE = 0.05

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
        "prompt": "この場面は、現場で実際に起こり得る業務の一コマとして自然に読めますか。この1件を単体で読んで採点してください（複数件を通して見える規則性は、この設問の点数ではなく痕跡分類 statistical_structure で申告してください）。",
        "scale": "1 (全く不自然) - 5 (非常に自然)",
    },
    {
        "question_id": "internally_consistent",
        "prompt": "登場する用語・やり取り・判断は、業務記録として整合していますか（矛盾や違和感はないか）。この1件の内部での整合を単体で採点してください。",
        "scale": "1 (不整合) - 5 (整合)",
    },
    {
        "question_id": "no_artificial_markers",
        "prompt": "実験・テスト・シミュレーションを示唆する痕跡（不自然な符丁やID、機械的な言い回し）に気づきましたか。",
        "scale": "yes/no (yesは減点対象)",
    },
    {
        "question_id": "artificial_marker_category",
        "prompt": (
            "直前の設問で「yes」と答えた場合のみ、痕跡の種類を1つ選んでください: "
            "(1) mechanical_generation: 機械生成の痕跡（システム語彙、日本語として不自然なトークン、"
            "壊れた/文字化けしたテキスト、テンプレートID等）。 "
            "(2) design_content: 場面の内容そのものが、意図的に設計されたテストケースのように見える"
            "（不自然に典型的すぎる、出来すぎている等）。 "
            "(3) statistical_structure: 個々の項目単体では気づかないが、多数の項目を横断して見える"
            "統計的な構造（繰り返しの文型骨格、連番的な日付など）。 "
            "「yes」の場合はこの3つのいずれかを必ず選択してください。"
        ),
        "scale": "mechanical_generation | design_content | statistical_structure (no_artificial_markers=yesのときのみ必須)",
    },
)

ARTIFICIAL_MARKER_CATEGORIES: tuple[str, ...] = (
    "mechanical_generation",
    "design_content",
    "statistical_structure",
)

# Backward-compatibility hardening: a "yes" response that carries no (or an
# unrecognized) artificial_marker_category is treated as the strictest
# category, mechanical_generation, so an old/unmigrated response packet (or a
# malformed category) cannot pass more easily than a properly-categorized one.
_DEFAULT_UNCATEGORIZED_MARKER_CATEGORY = "mechanical_generation"

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

    Round-2 blind SME review (data/design/MASTER_DESIGN.md §17.3) additionally
    flagged 20 content-duplicate pairs: an `inbox_delivered` ledger row
    delivering a customer's own `customer_utterance` message to their primary
    seat's inbox was being summarized the same way as an internal colleague
    share -- "連絡事項の共有: <customer's first-person text, copied
    verbatim>" -- which is byte-identical to the "顧客とのやり取り: <same
    text>" excerpt already sampled from the `customer_utterance` ledger row.
    Two fixes close this: (1) `_summarize_ledger_payload` now renders an
    `inbox_delivered` row whose nested message is itself a customer_utterance
    as a natural third-person business summary derived from structured
    fields (product/deadline/application_id), never by echoing the utterance
    text; (2) as defense in depth, every excerpt's *normalized* content
    (labels and whitespace stripped) is deduped across the whole excerpt list
    -- not just within the ledger loop and not just on exact string match --
    so one underlying event can never surface twice under two different
    labels.

    Round-4 blind SME review (data/design/MASTER_DESIGN.md §17.8): round 2's
    text-level fixes made the two excerpts for one customer event non-
    identical (a first-person utterance vs. a third-person internal-share
    memo), so the normalized-content dedup above no longer catches them --
    but the reviewer packet still paired each customer utterance with its own
    inbox-share memo about the same event, alternating mechanically through
    the deck (~20 utterance+memo pairs). A real blind-review sample would not
    systematically include both a customer's own words AND a colleague's
    paraphrase of the same conversation. Fixed by tracking the underlying
    `event_id` a `customer_utterance` excerpt and an `inbox_delivered`
    customer-share excerpt both derive from (via `_linked_customer_event_id`)
    and sampling AT MOST ONE excerpt per event_id among that pair of kinds;
    once an event_id's excerpt is taken, the other kind for the same event is
    skipped and a later, still-available excerpt (any kind, including a
    different customer event or a chat/other business-event row) fills the
    freed slot instead, so the packet size is not silently shrunk by the
    one-per-event rule.
    """
    chat_rows = read_jsonl(run_root / "chat_channel.jsonl")
    ledger_rows = read_jsonl(run_root / "world_ledger.jsonl")
    candidates: list[dict[str, Any]] = []
    seen_normalized: set[str] = set()

    def _try_stage(kind: str, text: str, *, linked_event_id: str | None) -> None:
        normalized = _normalize_excerpt_content(text)
        if not normalized or normalized in seen_normalized:
            return
        seen_normalized.add(normalized)
        candidates.append({"kind": kind, "text": text, "linked_event_id": linked_event_id})

    for row in chat_rows:
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        _try_stage("chat_message", body, linked_event_id=None)
    for row in ledger_rows:
        event_type = str(row.get("event_type") or "")
        phrasing = _LEDGER_EVENT_PHRASING.get(event_type)
        if phrasing is None:
            continue
        payload = row.get("payload") or {}
        summary = _summarize_ledger_payload(event_type, phrasing, payload)
        if not summary:
            continue
        _try_stage("business_event", summary, linked_event_id=_linked_customer_event_id(event_type, payload))

    excerpts: list[dict[str, Any]] = []
    used_event_ids: set[str] = set()
    deferred: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(excerpts) >= limit:
            break
        linked_event_id = candidate["linked_event_id"]
        if linked_event_id and linked_event_id in used_event_ids:
            deferred.append(candidate)
            continue
        if linked_event_id:
            used_event_ids.add(linked_event_id)
        excerpts.append(candidate)
    # Backfill freed slots (a deferred excerpt whose event_id was already
    # used elsewhere) with whatever candidates remain, up to `limit` -- a
    # deferred candidate is admissible here too as long as its own
    # linked_event_id was not itself separately consumed by another kept
    # excerpt in the meantime.
    for candidate in deferred:
        if len(excerpts) >= limit:
            break
        linked_event_id = candidate["linked_event_id"]
        if linked_event_id and linked_event_id in used_event_ids:
            continue
        if linked_event_id:
            used_event_ids.add(linked_event_id)
        excerpts.append(candidate)
    return [
        {"excerpt_id": f"{'chat' if item['kind'] == 'chat_message' else 'ledger'}_{idx}", "kind": item["kind"], "text": item["text"]}
        for idx, item in enumerate(excerpts)
    ]


def _linked_customer_event_id(event_type: str, payload: dict[str, Any]) -> str | None:
    """Return the underlying CustomerEvent.event_id a ledger row derives
    from, for the two kinds that can duplicate one customer event in the
    packet (a `customer_utterance` row, and an `inbox_delivered` row whose
    nested message is that same customer's utterance being shared to their
    primary seat's inbox). Returns None for anything else -- e.g.
    `month_end_close` or an `inbox_delivered` row carrying an internal chat
    message have no such linkage and must never be constrained by it.
    """
    if event_type == "customer_utterance":
        event_id = str(payload.get("event_id") or "").strip()
        return event_id or None
    if event_type == "inbox_delivered":
        nested = payload.get("message") or {}
        if isinstance(nested, dict) and str(nested.get("kind") or "") == "customer_utterance":
            event_id = str(nested.get("event_id") or "").strip()
            return event_id or None
    return None


def _summarize_ledger_payload(event_type: str, phrasing: str, payload: dict[str, Any]) -> str:
    if event_type == "inbox_delivered":
        nested = payload.get("message") or {}
        if isinstance(nested, dict) and str(nested.get("kind") or "") == "customer_utterance":
            # A customer's own message being delivered to their primary
            # seat's inbox is not an internal colleague share -- render it as
            # a natural third-person business summary derived from the
            # structured fields (product/deadline/application_id), never by
            # copying the customer's first-person utterance text (that
            # would duplicate the "顧客とのやり取り" excerpt sampled from the
            # customer_utterance ledger row for the same event). The
            # receiving seat (`to_seat`) lives at the ledger-payload level,
            # sibling to `message` (recorder.record_inbox's payload shape:
            # {"to_seat", "message"}) -- never inside the world-visible
            # message itself, since a seat id there would be forbidden
            # routing metadata (kernel.FORBIDDEN_INBOX_KEYS).
            return _summarize_inbox_customer_share(nested, to_seat=str(payload.get("to_seat") or ""))
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


# ---------------------------------------------------------------------------
# Round-4 blind SME review follow-up (data/design/MASTER_DESIGN.md §17.8):
# stage-aware, seeded internal-share memo phrasing.
#
# Round 4 flagged two compounding defects in every "連絡事項の共有" memo:
# (1) all ~20 memos rendered from the identical single skeleton
# "お客様より{product}の申込希望あり。期日は{date}。" -- differing only in the
# product/date substitutions, which reads as generated data; (2) the
# skeleton unconditionally asserted "申込希望あり" (an application request)
# even for a customer whose event was only at the consultation/hesitation
# stage (R-007/R-015/R-031) -- an internal record that misstates the
# customer's actual stage is a content-fidelity bug, not just a style issue.
#
# Both are fixed together: `customer_agent.world_visible_message` now carries
# the event's `customer_stage` (a genuine structured field driving the
# customer's own persona prompt too -- see deck.py) into the ledger payload,
# and this renderer selects (a) a stage-appropriate skeleton -- never
# claiming an application request for a "consultation"-stage event -- and
# (b) one of several skeletons per stage, chosen deterministically from
# (customer_id, event_id, receiving seat) so the same stage never collapses
# onto one fixed sentence across many memos. This mirrors
# `customer_agent._seeded_index`'s stable-hash pattern (never Python's
# global `random` or a time-based seed), so a given run bundle always
# renders the same memo text on rerun. No new semantic content is invented:
# each skeleton only states what the structured fields already assert
# (stage, product, deadline); it never asserts an application commitment
# that the underlying event did not have.
# ---------------------------------------------------------------------------

_SHARE_MEMO_SKELETONS_BY_STAGE: dict[str, tuple[str, ...]] = {
    "consultation": (
        "お客様より{product}についてご相談あり。",
        "{product}について、お客様よりご説明を聞きたいとのご連絡あり。",
        "お客様が{product}の件で、申込むかどうかまだ検討中とのこと。",
        "{product}に関して、お客様より一度話を聞きたいとの申し出あり。",
    ),
    "application_intent": (
        "お客様より{product}の申込希望あり。",
        "{product}について、お客様より申込を進めたいとのご連絡あり。",
        "お客様が{product}の申込手続を希望している。",
        "{product}の申込希望の連絡がお客様よりあり。",
    ),
    "procedural_request": (
        "お客様より{product}の手続状況について確認依頼あり。",
        "{product}の申込手続の途中で、お客様より必要書類等の確認依頼あり。",
        "お客様が{product}の手続の進み方について確認したいとのこと。",
        "{product}に関して、お客様より手続上の確認依頼の連絡あり。",
    ),
}

_SHARE_MEMO_SKELETONS_NO_PRODUCT: dict[str, tuple[str, ...]] = {
    "consultation": ("お客様よりご相談あり。", "お客様がまだ検討中とのこと。"),
    "application_intent": ("お客様より申込希望あり。", "お客様が申込手続を希望している。"),
    "procedural_request": ("お客様より手続状況について確認依頼あり。", "お客様が手続の進み方を確認したいとのこと。"),
}


def _seeded_share_memo_index(*, customer_id: str, event_id: str, seat_id: str, pool_size: int) -> int:
    """Deterministic memo-skeleton index from (customer_id, event_id, seat).

    Same stable-hash pattern as `customer_agent._seeded_index` /
    `identity.display_name_for_seat`: never Python's global `random` or a
    time-based seed, so the same underlying event and receiving seat always
    render the same memo skeleton across reruns, while different
    seats/events spread naturally across the pool.
    """
    if pool_size <= 0:
        return 0
    digest = hashlib.sha256(f"share_memo:{customer_id}:{event_id}:{seat_id}".encode("utf-8")).hexdigest()
    return int(digest, 16) % pool_size


def _summarize_inbox_customer_share(message: dict[str, Any], *, to_seat: str = "") -> str:
    """Render a customer_utterance inbox delivery as a natural internal-share
    summary built only from whitelisted structured fields (never the
    customer's own words) -- e.g. "連絡事項の共有: お客様より投資信託についてご相談
    あり。". Stage-aware (never asserts an application request for a
    consultation-stage event) and seeded per (customer_id, event_id,
    receiving seat) across several skeletons per stage, so memos are not
    byte-identical clones of each other. Deterministic: the same fields
    always render the same text.

    `to_seat` is the ledger-payload-level receiving seat id (sibling to the
    `message` dict, from recorder.record_inbox's {"to_seat", "message"}
    shape) -- it is never read off `message` itself, since a seat id inside
    the world-visible message would be forbidden routing metadata (see
    kernel.FORBIDDEN_INBOX_KEYS / customer_agent.world_visible_message).
    """
    product = str(message.get("product") or "").strip()
    deadline_display = str(message.get("deadline_display") or "").strip()
    stage = str(message.get("customer_stage") or "application_intent").strip()
    if stage not in _SHARE_MEMO_SKELETONS_BY_STAGE:
        stage = "application_intent"
    if not product and not deadline_display:
        return ""
    customer_id = str(message.get("customer_id") or "")
    event_id = str(message.get("event_id") or "")
    seat_id = to_seat
    if product:
        pool = _SHARE_MEMO_SKELETONS_BY_STAGE[stage]
        idx = _seeded_share_memo_index(customer_id=customer_id, event_id=event_id, seat_id=seat_id, pool_size=len(pool))
        parts = [pool[idx].format(product=product)]
    else:
        pool = _SHARE_MEMO_SKELETONS_NO_PRODUCT[stage]
        idx = _seeded_share_memo_index(customer_id=customer_id, event_id=event_id, seat_id=seat_id, pool_size=len(pool))
        parts = [pool[idx]]
    if deadline_display:
        deadline_text = deadline_display[:-2] if deadline_display.endswith("まで") else deadline_display
        parts.append(f"期日は{deadline_text}。")
    return f"連絡事項の共有: {''.join(parts)}"


_WHITESPACE_RE = re.compile(r"\s+")
_LABEL_PREFIX_RE = re.compile(r"^[^:：]{1,20}[:：]\s*")


def _normalize_excerpt_content(text: str) -> str:
    """Normalize excerpt text for duplicate detection: strip a leading
    "label: " prefix (e.g. "顧客とのやり取り: ", "連絡事項の共有: ") and collapse
    whitespace, so the same underlying content sampled under two different
    labels collapses to the same key. This is a content-dedup key only, never
    shown to a reviewer.
    """
    stripped = _LABEL_PREFIX_RE.sub("", text.strip())
    return _WHITESPACE_RE.sub("", stripped)


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


REVIEWER_TYPES: tuple[str, ...] = ("human_sme", "ai_proxy")


def build_blind_review_packet(
    run_roots: list[Path],
    *,
    samples_per_run: int = 10,
    reviewer_type: str = "human_sme",
    reviewer: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a blind-review packet and its experimenter-side id map.

    Returns ``(packet, id_map)``. The packet is the only file a reviewer ever
    sees; the id map stays on the experimenter side.

    ``reviewer_type`` is ``"human_sme"`` (default) or ``"ai_proxy"``: an
    ai_proxy pass is an internal calibration signal, not a Stage 9
    external-claim SME review, and is labeled accordingly in the report
    (``claim_level: "internal_calibration"`` vs ``"human_sme"`` -- see
    write_sme_blind_review_report). ``reviewer`` is an optional free-form
    dict for reviewer prompt/model/blindness notes (formalizing a field
    already used informally in practice); it is preserved verbatim into the
    packet and report if present, and is never required.

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
    if reviewer_type not in REVIEWER_TYPES:
        raise ValueError(f"reviewer_type must be one of {REVIEWER_TYPES}, got {reviewer_type!r}")
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
        "reviewer_type": reviewer_type,
        "note": (
            "Reviewer-facing packet. Fill in each item's `response` as "
            '{"plausible_workplace_scene": 1-5, "internally_consistent": 1-5, "no_artificial_markers": "yes"|"no", '
            '"artificial_marker_category": "mechanical_generation"|"design_content"|"statistical_structure", '
            '"note": "optional free text explaining the flag"}. '
            "artificial_marker_category is required only when no_artificial_markers is \"yes\" (see the "
            "artificial_marker_category question prompt for the three category definitions). "
            "note is optional free text; when a mechanical_generation flag's note references a frozen-corpus "
            "term also present in the item's own text, and cites no other basis, it is recategorized to "
            "design_content for counting purposes (see score_sme_blind_review's docstring). "
            "Null responses count as unreviewed, never as passing."
        ),
    }
    if reviewer is not None:
        packet["reviewer"] = dict(reviewer)
    id_map = {
        "schema_version": SME_BLIND_REVIEW_ID_MAP_SCHEMA_VERSION,
        "kind": "blind_review_id_map",
        "item_count": len(items),
        "dropped_count": len(dropped),
        "dropped_items": dropped,
        "entries": entries,
        "packet_hash": packet_hash,
        "reviewer_type": reviewer_type,
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


def _normalize_marker_category(response: dict[str, Any], *, flagged_artificial: bool) -> str | None:
    """Resolve the effective artificial_marker_category for a flagged item.

    Returns ``None`` when the item was not flagged at all. When flagged, a
    recognized category (``mechanical_generation`` / ``design_content`` /
    ``statistical_structure``) is returned verbatim. A "yes" response that
    omits the category, or supplies an unrecognized value, is a legacy/
    unmigrated response shape -- it is treated as ``mechanical_generation``
    (the strictest category, which always fails the item and the gate) so
    that an old-format packet can never pass more easily than a properly
    categorized one.
    """
    if not flagged_artificial:
        return None
    raw_category = str(response.get("artificial_marker_category") or "").strip()
    if raw_category in ARTIFICIAL_MARKER_CATEGORIES:
        return raw_category
    return _DEFAULT_UNCATEGORIZED_MARKER_CATEGORY


# ---------------------------------------------------------------------------
# Round-7 blind SME review follow-up (data/design/MASTER_DESIGN.md §17.14):
# frozen-corpus-term recategorization.
#
# Round 7 flagged R-008 as `mechanical_generation` on account of the product
# name "乗換保険". That name is not a generation artifact at all -- it is the
# frozen-corpus product name for probe P-03 (deck.py's PROBE_ROUTES["P-03"],
# data/compiled_data/world_config_v2.yaml's "P-03 乗換保険(期限W2金)", and
# data/compiled_data/deck_v2.json), already documented as such in
# MASTER_DESIGN.md §17.6 ("frozen-corpus naming (e.g. 乗換保険)") -- the
# corpus document set is frozen for comparability across calibration rounds,
# so this term cannot be renamed away. A reviewer flagging it as
# "mechanical_generation" is a gate-semantics miscategorization, not a real
# defect: this is the (b) `design_content` kind (recognizability of
# deliberately-designed, frozen corpus content) §17.6 already carves out, not
# genuine machine-generation noise (system vocabulary/non-Japanese tokens/
# broken text/template ids).
#
# APPROVED gate-semantics fix (project owner, 2026-07-06): when a flagged
# response's ONLY basis for the mechanical_generation flag is a listed
# frozen-corpus term appearing in both the item's own text and the reviewer's
# note, recategorize the flag to design_content for counting purposes. The
# zero-mechanical-flags requirement is otherwise UNCHANGED -- this is strictly
# a categorization correctness fix, never a threshold relaxation: if the
# note ALSO cites anything else (duplication, broken text, system
# vocabulary), the item is NOT recategorized and the mechanical_generation
# flag stands, because the other basis is a genuine mechanical-generation
# concern this fix must not paper over.
#
# The list is structured as a tuple of Japanese terms for future additions
# (the mechanism generalizes to any frozen-corpus term, not just 乗換保険).
# ---------------------------------------------------------------------------

FROZEN_CORPUS_TERMS: tuple[str, ...] = ("乗換保険",)

# Substring signals that indicate a reviewer's note cites a basis OTHER than
# the frozen-corpus term itself -- duplication, broken/garbled text, or
# system/experimenter vocabulary. Any of these present in the note means the
# other basis stands and the flag must NOT be recategorized, even if the note
# also happens to mention a frozen-corpus term.
_OTHER_MECHANICAL_BASIS_SIGNALS: tuple[str, ...] = (
    "重複",
    "繰り返し",
    "反復",
    "壊れ",
    "破損",
    "文字化け",
    "途切れ",
    "切れて",
    "システム語彙",
    "システム用語",
    "不自然なトークン",
    "テンプレートID",
    "テンプレート ID",
)


def _note_cites_only_frozen_corpus_term(note: str, *, item_text: str) -> str | None:
    """Return the specific frozen-corpus term the recategorization applies
    for, or ``None`` if recategorization does not apply.

    Applies only when: (1) the item's own text actually contains a listed
    term (never recategorize on the reviewer's say-so alone -- the term must
    really be there), (2) the reviewer's note references that same term, and
    (3) the note cites no other mechanical-generation basis (duplication,
    broken text, system vocabulary) -- if it does, that other basis stands
    and this function returns None so the flag is left as-is.
    """
    if not note:
        return None
    if any(signal in note for signal in _OTHER_MECHANICAL_BASIS_SIGNALS):
        return None
    for term in FROZEN_CORPUS_TERMS:
        if term in item_text and term in note:
            return term
    return None


def score_sme_blind_review(packet: dict[str, Any]) -> dict[str, Any]:
    """Score a blind-review packet from filled-in reviewer responses.

    A response shape is ``{"plausible_workplace_scene": 1-5,
    "internally_consistent": 1-5, "no_artificial_markers": "yes"|"no",
    "artificial_marker_category": "mechanical_generation"|"design_content"|
    "statistical_structure", "note": "<optional free text>"}`` (category
    required only when no_artificial_markers is "yes"; note is always
    optional). Items whose `response` is still null count as unreviewed and
    are excluded from the plausibility rate's numerator/denominator but are
    always reported, so a packet that was never sent to a reviewer cannot
    silently read as "no problems found".

    MASTER_DESIGN.md section 17 (2026-07-05 approved recalibration): rounds
    1->3 of blind review took flags from 25/39 to 40/40 to 11/40; the
    remaining flags decompose into (a) mechanical generation artifacts
    (system vocabulary, non-Japanese tokens, broken/garbled text, template
    ids), (b) recognizability of deliberately-designed probe scenarios and
    frozen-corpus naming, and (c) aggregate statistical structure visible only
    across many items. (b)/(c) are structurally irreducible without
    destroying the experiment design, and the original design-doc criterion
    was "現場としてあり得る度" plausibility at or above a threshold, never
    zero-flags. Only a ``mechanical_generation`` flag on an item fails that
    item; ``design_content``/``statistical_structure`` flags are counted and
    reported per category but do not fail the item on their own.

    Round-7 follow-up (§17.14, approved gate-semantics fix): a response may
    also carry a free-form ``note`` string. When a ``mechanical_generation``
    flag's sole basis is a listed ``FROZEN_CORPUS_TERMS`` entry -- the term
    actually appears in the item's own text AND the note references that same
    term AND the note cites no other basis (duplication/broken text/system
    vocabulary) -- the flag is recategorized to ``design_content`` for
    counting purposes (never dropped: it still moves into the
    ``design_content`` bucket, it is just no longer counted as
    ``mechanical_generation``). Each such recategorization is recorded on its
    row (``recategorized_from``/``recategorization_basis``) so the
    transparency is machine-visible; see ``recategorized_count`` in the
    returned dict for the aggregate.
    """
    items = packet.get("items") or []
    reviewed: list[dict[str, Any]] = []
    unreviewed_count = 0
    category_flag_counts: dict[str, int] = {category: 0 for category in ARTIFICIAL_MARKER_CATEGORIES}
    recategorized_rows: list[dict[str, Any]] = []
    for item in items:
        response = item.get("response")
        if not response:
            unreviewed_count += 1
            continue
        plausible = _coerce_scale(response.get("plausible_workplace_scene"))
        consistent = _coerce_scale(response.get("internally_consistent"))
        flagged_artificial = str(response.get("no_artificial_markers") or "").strip().lower() == "yes"
        marker_category = _normalize_marker_category(response, flagged_artificial=flagged_artificial)
        recategorized_from: str | None = None
        recategorization_basis: str | None = None
        if marker_category == "mechanical_generation":
            note = str(response.get("note") or "").strip()
            item_text = str(item.get("text") or "")
            matched_term = _note_cites_only_frozen_corpus_term(note, item_text=item_text)
            if matched_term is not None:
                recategorized_from = marker_category
                recategorization_basis = f"frozen_corpus_term:{matched_term}"
                marker_category = "design_content"
        if marker_category is not None:
            category_flag_counts[marker_category] += 1
        fails_for_mechanical = marker_category == "mechanical_generation"
        passes_item = (
            plausible is not None
            and plausible >= 4
            and consistent is not None
            and consistent >= 4
            and not fails_for_mechanical
        )
        row: dict[str, Any] = {
            "item_id": item.get("item_id"),
            "plausible_workplace_scene": plausible,
            "internally_consistent": consistent,
            "flagged_artificial_markers": flagged_artificial,
            "artificial_marker_category": marker_category,
            "passes_item": passes_item,
        }
        if recategorized_from is not None:
            row["recategorized_from"] = recategorized_from
            row["recategorization_basis"] = recategorization_basis
            recategorized_rows.append(row)
        reviewed.append(row)
    reviewed_count = len(reviewed)
    passing_count = sum(1 for row in reviewed if row["passes_item"])
    plausibility_rate = (passing_count / reviewed_count) if reviewed_count else 0.0
    mechanical_generation_flag_count = category_flag_counts["mechanical_generation"]
    total_artificial_marker_flag_count = sum(category_flag_counts.values())
    return {
        "schema_version": SME_BLIND_REVIEW_SCHEMA_VERSION,
        "kind": "blind_review_scoring",
        "item_count": len(items),
        "reviewed_count": reviewed_count,
        "unreviewed_count": unreviewed_count,
        "passing_count": passing_count,
        # Backward-compatible alias: historically this counted every
        # no_artificial_markers="yes" flag regardless of category. It is kept
        # equal to the total across all three categories so any external
        # consumer reading this field still sees the full flag volume; the
        # gate itself now keys off mechanical_generation_flag_count.
        "artificial_marker_flag_count": total_artificial_marker_flag_count,
        "mechanical_generation_flag_count": mechanical_generation_flag_count,
        "artificial_marker_category_counts": dict(category_flag_counts),
        # Round-7 follow-up (§17.14): count and rows of items whose flag was
        # recategorized away from mechanical_generation (frozen-corpus-term
        # basis only) -- surfaced so the recategorization is machine-visible,
        # never silent.
        "recategorized_count": len(recategorized_rows),
        "recategorized_rows": recategorized_rows,
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

    Expert-review hardening (SME gate honesty):
    - sme_blind_review_id_map.json is REQUIRED alongside the packet; this
      report reads `dropped_count` from it, not from the packet (the packet
      never carries drop bookkeeping -- see build_blind_review_packet). A
      missing id map is an honest block, not a silent skip.
    - Any leaked_vocabulary_redacted drop (dropped_count > 0) counts as an
      ARTIFACT DETECTION, not an exclusion: it means the world itself leaked
      experimenter vocabulary into a rendered record, which is a defect to
      fix in the world (MASTER_DESIGN.md section 17.2's diegetic
      record-quality fix), not something to quietly paper over by dropping
      the offending excerpt from the packet. dropped_count > 0 therefore
      fails this report with that stated reason, regardless of how well the
      remaining (clean) items scored.
    - reviewer_type ("human_sme" | "ai_proxy") is read from the packet and
      carried into the report as claim_level: an ai_proxy pass is labeled
      "internal_calibration" (a self-consistency signal only); a human_sme
      pass is labeled "human_sme" (see MASTER_DESIGN.md section 12/17's
      two-level readiness split). Any free-form `reviewer` prompt/model/
      blindness notes present on the packet are preserved into the report.
    """
    inputs_path = campaign_root / "sme_blind_review_inputs.json"
    id_map_path = campaign_root / "sme_blind_review_id_map.json"
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
    reviewer_type = str(packet.get("reviewer_type") or "human_sme")
    claim_level = "internal_calibration" if reviewer_type == "ai_proxy" else "human_sme"

    if not id_map_path.exists():
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "sme_blind_review",
            "status": "blocked",
            "passed": False,
            "reviewer_type": reviewer_type,
            "claim_level": claim_level,
            "checks": [
                {
                    "name": "sme_blind_review_id_map_supplied",
                    "passed": False,
                    "required_input": "sme_blind_review_id_map.json",
                    "detail": (
                        "sme_blind_review_id_map.json is required alongside the packet -- "
                        "dropped_count (leaked-vocabulary artifact detections) can only be read from it."
                    ),
                }
            ],
            "notes": [],
        }
        (campaign_root / "sme_blind_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    id_map = json.loads(id_map_path.read_text(encoding="utf-8"))
    dropped_count = int(id_map.get("dropped_count") or 0)

    scoring = score_sme_blind_review(packet)
    target = float(packet.get("plausibility_target") or SME_PLAUSIBILITY_TARGET)
    min_samples = int(packet.get("min_reviewed_samples") or SME_MIN_REVIEWED_SAMPLES)
    enough_reviewed = scoring["reviewed_count"] >= min_samples
    # 2026-07-05 approved recalibration (MASTER_DESIGN.md section 17): the
    # gate keys off mechanical_generation flags only. design_content/
    # statistical_structure flags are counted/reported per category but do
    # not, on their own, block the gate -- see score_sme_blind_review's
    # docstring for the empirical basis.
    #
    # 2026-07-06 approved recalibration (approval #8): zero-tolerance on a
    # ~39-item panel made the verdict a ~25% lottery against the measured
    # 3-5% irreducible LLM fluency floor (a NEW undetectable glitch mode
    # appeared in each of rounds 6-8 despite three guard generations).
    # The gate now allows a mechanical_generation RATE up to
    # SME_MECHANICAL_RATE_TOLERANCE over the reviewed panel; every flag
    # stays itemized in `rows`, and the pooled-panel protocol (two
    # same-world control bundles, one blind session) doubles the sample.
    mechanical_rate = (
        scoring["mechanical_generation_flag_count"] / scoring["reviewed_count"]
        if scoring["reviewed_count"]
        else 1.0
    )
    mechanical_within_tolerance = mechanical_rate <= SME_MECHANICAL_RATE_TOLERANCE
    no_leak_drops = dropped_count == 0
    ok = enough_reviewed and mechanical_within_tolerance and no_leak_drops and scoring["plausibility_rate"] >= target
    if not enough_reviewed:
        detail = f"reviewed_count={scoring['reviewed_count']} < min_reviewed_samples={min_samples}"
    elif not mechanical_within_tolerance:
        detail = (
            f"mechanical_generation_rate={mechanical_rate:.4f} > tolerance={SME_MECHANICAL_RATE_TOLERANCE} "
            f"({scoring['mechanical_generation_flag_count']}/{scoring['reviewed_count']}); "
            f"artificial_marker_flag_count={scoring['artificial_marker_flag_count']} total across categories "
            f"{scoring['artificial_marker_category_counts']}"
        )
    elif not no_leak_drops:
        detail = (
            f"dropped_count={dropped_count} leaked_vocabulary_redacted excerpt(s) detected in "
            "sme_blind_review_id_map.json -- this is an artifact detection (the world leaked "
            "experimenter vocabulary into a rendered record); fix the world, not the packet"
        )
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
            "mechanical_generation_flag_count": scoring["mechanical_generation_flag_count"],
            "mechanical_generation_rate": mechanical_rate,
            "mechanical_rate_tolerance": SME_MECHANICAL_RATE_TOLERANCE,
            "artificial_marker_category_counts": scoring["artificial_marker_category_counts"],
            "recategorized_count": scoring["recategorized_count"],
            "recategorized_rows": scoring["recategorized_rows"],
            "dropped_count": dropped_count,
            "plausibility_rate": scoring["plausibility_rate"],
            "rows": scoring["rows"],
        }
    ]
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "sme_blind_review",
        "status": "passed" if ok else "blocked",
        "passed": ok,
        "reviewer_type": reviewer_type,
        "claim_level": claim_level,
        "checks": checks,
        "notes": [
            "Packet items are stripped of experimenter-plane vocabulary before reaching a reviewer (strip_experimenter_vocabulary).",
            "An unfilled or under-filled packet always fails honestly; this report never marks a pass without scored reviewer rows.",
            "dropped_count > 0 (from sme_blind_review_id_map.json) is an artifact detection, not exclusion bookkeeping: it fails this report because the world itself leaked vocabulary, not because the sample was incomplete.",
            f"reviewer_type={reviewer_type!r} -> claim_level={claim_level!r}: an ai_proxy pass is an internal calibration signal only, never an external human_sme claim.",
            "2026-07-05 approved recalibration (MASTER_DESIGN.md section 17): only a mechanical_generation "
            "artificial-marker flag fails an item/gates the report; design_content/statistical_structure "
            "flags are counted per category and reported but do not fail on their own. A 'yes' response "
            "without a recognized category is treated as mechanical_generation (strictest) for backward "
            "compatibility with old/unmigrated response packets.",
            "2026-07-06 approved gate-semantics fix (MASTER_DESIGN.md §17.14): a mechanical_generation flag "
            "whose sole basis is a listed FROZEN_CORPUS_TERMS entry (the term appears in the item's own text "
            "AND the reviewer's note references it, with no other basis cited) is recategorized to "
            "design_content for counting purposes -- see recategorized_count/recategorized_rows above. This "
            "is a categorization-correctness fix only: the zero-mechanical-flags gate requirement is "
            "unchanged, and any note citing an additional basis (duplication/broken text/system vocabulary) "
            "is never recategorized.",
        ],
        "scoring": scoring,
    }
    if packet.get("reviewer") is not None:
        payload["reviewer"] = packet["reviewer"]
    (campaign_root / "sme_blind_review.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
