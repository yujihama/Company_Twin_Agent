"""Stage 9 evidence manifest (MASTER_DESIGN.md section 12/17, expert-review
hardening pass).

The expert review of Stage 9 readiness found a concrete false-green hole:
every readiness evidence report (backcasting/SME/holdout/g3) can be
individually well-formed while still not sharing a common provenance story --
different corpus versions, different judge backends, an SME reviewer field
that is never checked against the packet actually scored, a holdout plan
whose spec_hash is never verified against the run bundle it claims to score.

`build_stage9_evidence_manifest()` binds every readiness evidence artifact to
its provenance in one place: the git commit that generated the manifest, the
command line, the campaign root, and per-evidence-class entries (run roots +
their meta.json timing, corpus/mutation hashes, prompt_mode, seat model
bindings, judge backend/model/prompt_version, backcasting sample seed/hash,
SME packet hash/reviewer_type, holdout plan hash/injection spec_hashes).

World-version heterogeneity (different corpus hashes across evidence
classes) is RECORDED loudly in a top-level `world_versions` section rather
than silently averaged away or hidden -- see MASTER_DESIGN.md section 17.3.

`readiness.py`'s `stage9_evidence_manifest_consistent` check re-derives the
same provenance fields from the *current* report files and requires them to
match what the manifest recorded; a mismatch or a missing manifest means the
overall gate cannot reach 10/10.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

EVIDENCE_MANIFEST_SCHEMA_VERSION = "company_twin.stage9_evidence_manifest.v1"
EVIDENCE_MANIFEST_FILENAME = "stage9_evidence_manifest.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _git_commit(root: Path) -> str:
    """Best-effort git commit hash of the code generating this manifest.

    Never raises: an unavailable git binary or a non-repo checkout (e.g. a
    packaged distribution) must not prevent the manifest from being written --
    it is recorded as "unknown" instead, which downstream consistency checks
    treat as a (visible) provenance gap rather than a crash.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _meta_timing(run_root: Path) -> dict[str, Any]:
    meta = _read_json(run_root / "meta.json")
    return {
        "run_root": run_root.name,
        "stage": meta.get("stage"),
        "started": meta.get("created_at") or meta.get("started"),
        "completed": meta.get("completed_at") or meta.get("completed"),
        "prompt_mode": meta.get("prompt_mode"),
        "mutation_ids": meta.get("mutation_ids"),
        "mutation_hash": meta.get("mutation_hash"),
        "effective_corpus_hash": meta.get("effective_corpus_hash"),
    }


def _config_hashes(run_root: Path) -> dict[str, Any]:
    config = _read_json(run_root / "config.json")
    corpus = ((config.get("world") or {}).get("corpus") or {})
    return {
        "raw_corpus_hash": corpus.get("raw_corpus_hash"),
        "effective_corpus_hash": corpus.get("effective_corpus_hash"),
        "mutation_hash": corpus.get("mutation_hash"),
        "mutation_count": corpus.get("mutation_count"),
    }


def _seat_model_bindings(run_root: Path) -> dict[str, Any]:
    config = _read_json(run_root / "config.json")
    population = ((config.get("world") or {}).get("population") or {})
    return population.get("binding") or {}


def _resolve_run_roots(campaign_root: Path, names: list[str]) -> list[Path]:
    return [campaign_root / name for name in names if (campaign_root / name).exists()]


def _discover_s2_run_roots(campaign_root: Path) -> list[Path]:
    if not campaign_root.exists():
        return []
    roots = []
    for path in sorted(campaign_root.iterdir()):
        if not path.is_dir() or not (path / "meta.json").exists():
            continue
        meta = _read_json(path / "meta.json")
        if meta.get("stage") in ("S1", "S2"):
            roots.append(path)
    return roots


def _backcasting_evidence(campaign_root: Path) -> dict[str, Any]:
    results = _read_json(campaign_root / "backcasting_resimulation_results.json")
    if not results:
        return {"present": False, "detail": "backcasting_resimulation_results.json missing"}
    sample = results.get("sample") or {}
    selected_case_ids = list(sample.get("selected_case_ids") or [])
    judge = results.get("judge") or {}
    seat = results.get("seat") or {}
    run_roots = [str((row or {}).get("run_root") or "") for row in (results.get("results") or [])]
    run_roots = [name for name in run_roots if name]
    return {
        "present": True,
        "run_roots": run_roots,
        "sample_size": sample.get("sample_size"),
        "sample_seed": sample.get("sample_seed"),
        "population_size": sample.get("population_size"),
        "selected_case_ids_sha256": _sha256_of_list(selected_case_ids),
        "selected_case_id_count": len(selected_case_ids),
        "prompt_mode": "backcasting_seat_prompt",
        "seat_model_bindings": {seat.get("seat_id"): seat.get("model")} if seat.get("seat_id") else {},
        "judge": {
            "backend": judge.get("backend"),
            "model": judge.get("model"),
            "prompt_version": judge.get("prompt_version"),
            "readiness_eligible": judge.get("readiness_eligible"),
        },
    }


def _sha256_of_list(values: list[str]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(list(values), ensure_ascii=False, sort_keys=False).encode("utf-8")).hexdigest()


def _sme_evidence(campaign_root: Path) -> dict[str, Any]:
    packet = _read_json(campaign_root / "sme_blind_review_inputs.json")
    id_map = _read_json(campaign_root / "sme_blind_review_id_map.json")
    if not packet:
        return {"present": False, "detail": "sme_blind_review_inputs.json missing"}
    run_roots = sorted({str(entry.get("run_root") or "") for entry in (id_map.get("entries") or []) if entry.get("run_root")})
    return {
        "present": True,
        "packet_hash": packet.get("packet_hash"),
        "id_map_present": bool(id_map),
        "id_map_packet_hash": id_map.get("packet_hash"),
        "dropped_count": id_map.get("dropped_count"),
        "reviewer_type": packet.get("reviewer_type"),
        "reviewer": packet.get("reviewer"),
        "run_roots": run_roots,
        "item_count": packet.get("item_count"),
    }


def _holdout_evidence(campaign_root: Path) -> dict[str, Any]:
    plan = _read_json(campaign_root / "holdout_inputs.json")
    if not plan:
        return {"present": False, "detail": "holdout_inputs.json missing"}
    injections = plan.get("injections") or []
    spec_hashes = {str(injection.get("injection_id") or ""): injection.get("spec_hash") for injection in injections}
    run_roots = sorted({name for injection in injections for name in (injection.get("planned_run_roots") or [])})
    return {
        "present": True,
        "plan_hash": plan.get("plan_hash"),
        "injection_count": plan.get("injection_count"),
        "spec_hashes": spec_hashes,
        "planned_run_roots": run_roots,
    }


def _g3_evidence(campaign_root: Path) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    for path in sorted(campaign_root.glob("**/g3_semantic_grounding.json")):
        payload = _read_json(path)
        if not payload:
            continue
        judge = payload.get("judge") or {}
        reports.append(
            {
                "path": str(path.relative_to(campaign_root)).replace("\\", "/"),
                "judge_backend": judge.get("backend"),
                "judge_model": judge.get("model"),
                "judge_prompt_version": judge.get("prompt_version"),
                "readiness_eligible": judge.get("readiness_eligible"),
            }
        )
    return {"present": bool(reports), "reports": reports}


def build_stage9_evidence_manifest(campaign_root: Path, *, command_line: list[str] | None = None, code_root: Path | None = None) -> dict[str, Any]:
    """Build the Stage 9 evidence manifest binding every readiness evidence
    artifact to its provenance.

    This is a pure read/derive step over whatever evidence files already
    exist under `campaign_root` -- it does not run any simulation, judge, or
    network call. Missing evidence is recorded as `present: false`, never
    fabricated.
    """
    campaign_root = campaign_root.resolve()
    root_for_git = (code_root or Path(__file__).resolve().parents[2])

    backcasting = _backcasting_evidence(campaign_root)
    sme = _sme_evidence(campaign_root)
    holdout = _holdout_evidence(campaign_root)
    g3 = _g3_evidence(campaign_root)

    backcasting_run_root_details = [
        _meta_timing(campaign_root / "backcasting_runs" / case_id) | _config_hashes(campaign_root / "backcasting_runs" / case_id)
        for case_id in _case_ids_from_run_roots(backcasting.get("run_roots") or [])
    ]

    holdout_run_root_details = [
        _meta_timing(root) | _config_hashes(root)
        for root in _resolve_run_roots(campaign_root, holdout.get("planned_run_roots") or [])
    ]

    sme_run_root_details = [
        _meta_timing(root) | _config_hashes(root)
        for root in _resolve_run_roots(campaign_root, sme.get("run_roots") or [])
    ]

    s2_run_roots = _discover_s2_run_roots(campaign_root)
    s2_run_root_details = [
        _meta_timing(root) | _config_hashes(root) | {"seat_model_bindings": _seat_model_bindings(root)}
        for root in s2_run_roots
    ]

    world_versions = _world_versions_section(
        {
            "backcasting": backcasting_run_root_details,
            "holdout": holdout_run_root_details,
            "sme_blind_review": sme_run_root_details,
            "s2_bundles": s2_run_root_details,
        }
    )

    manifest = {
        "schema_version": EVIDENCE_MANIFEST_SCHEMA_VERSION,
        "kind": "stage9_evidence_manifest",
        "campaign_root": str(campaign_root),
        "git_commit": _git_commit(root_for_git),
        "command_line": list(command_line) if command_line is not None else list(sys.argv),
        "evidence": {
            "backcasting": {**backcasting, "run_root_details": backcasting_run_root_details},
            "sme_blind_review": {**sme, "run_root_details": sme_run_root_details},
            "holdout": {**holdout, "run_root_details": holdout_run_root_details},
            "g3_semantic_grounding": g3,
            "s2_bundles": {"run_roots": [root.name for root in s2_run_roots], "run_root_details": s2_run_root_details},
        },
        "world_versions": world_versions,
        "note": (
            "This manifest binds every Stage 9 readiness evidence artifact to its provenance "
            "(git commit, command line, corpus/mutation hashes, judge backend/model, sample seed). "
            "World-version heterogeneity across evidence classes is recorded in world_versions, "
            "not silently hidden. readiness.py's stage9_evidence_manifest_consistent check requires "
            "this manifest to exist AND match the current report files; absence or mismatch means "
            "the gate cannot reach 10/10."
        ),
    }
    return manifest


def _case_ids_from_run_roots(run_roots: list[str]) -> list[str]:
    """backcasting result rows carry run_root as an absolute path string
    (see backcasting_run.py's `_failed_row`/`_run_one_case`: `run_root: str(run_root)`).
    Extract just the trailing case_id directory name for re-resolution under
    the current campaign_root, since the manifest may be built from a
    different working directory/host than the one that ran the resimulation."""
    names = []
    for raw in run_roots:
        name = Path(raw).name
        if name:
            names.append(name)
    return names


def _world_versions_section(evidence_details: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Record distinct effective_corpus_hash values observed per evidence
    class, and flag heterogeneity across classes loudly rather than hiding it.
    """
    by_class: dict[str, list[str]] = {}
    all_hashes: set[str] = set()
    for evidence_class, details in evidence_details.items():
        hashes = sorted({str(detail.get("effective_corpus_hash") or "") for detail in details if detail.get("effective_corpus_hash")})
        if hashes:
            by_class[evidence_class] = hashes
            all_hashes |= set(hashes)
    return {
        "distinct_effective_corpus_hashes": sorted(all_hashes),
        "distinct_hash_count": len(all_hashes),
        "by_evidence_class": by_class,
        "heterogeneous": len(all_hashes) > 1,
        "note": (
            "heterogeneous=true means different evidence classes (or different run bundles within "
            "one class) were produced under different effective_corpus_hash values -- e.g. a "
            "pre-#26 world calibration mixed with post-fix evidence. This is recorded, not hidden; "
            "see MASTER_DESIGN.md section 17 for the diegetic record-quality world-version change."
        ),
    }


def write_stage9_evidence_manifest(campaign_root: Path, *, command_line: list[str] | None = None, code_root: Path | None = None) -> dict[str, Any]:
    campaign_root = campaign_root.resolve()
    campaign_root.mkdir(parents=True, exist_ok=True)
    manifest = build_stage9_evidence_manifest(campaign_root, command_line=command_line, code_root=code_root)
    (campaign_root / EVIDENCE_MANIFEST_FILENAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# Fields compared between the recorded manifest and a freshly-rebuilt one to
# decide "consistent with the current report files". This is intentionally
# narrower than a full deep-equal: run_root_details timing fields are allowed
# to be absent/None in both without causing a mismatch, but any provenance
# field that IS present in the current evidence and differs from what the
# manifest recorded is a genuine drift (e.g. someone re-ran sme-pack with a
# different reviewer_type after the manifest was written, or a holdout plan
# was rebuilt with a different spec_hash).
_CONSISTENCY_FIELDS: dict[str, tuple[str, ...]] = {
    "backcasting": ("sample_size", "sample_seed", "selected_case_ids_sha256", "judge"),
    "sme_blind_review": ("packet_hash", "reviewer_type", "dropped_count"),
    "holdout": ("plan_hash", "spec_hashes"),
}


def check_manifest_consistency(campaign_root: Path) -> dict[str, Any]:
    """Re-derive the same provenance fields from the *current* report files
    and compare them against what stage9_evidence_manifest.json recorded.

    Returns a dict with `passed` (manifest exists AND every checked field
    matches) and `mismatches` (a list of human-readable field diffs). This is
    a pure read-only comparison; it never rewrites the manifest.
    """
    campaign_root = campaign_root.resolve()
    manifest_path = campaign_root / EVIDENCE_MANIFEST_FILENAME
    if not manifest_path.exists():
        return {"passed": False, "manifest_present": False, "mismatches": [], "detail": f"{EVIDENCE_MANIFEST_FILENAME} missing"}
    recorded = _read_json(manifest_path)
    if recorded.get("schema_version") != EVIDENCE_MANIFEST_SCHEMA_VERSION:
        return {
            "passed": False,
            "manifest_present": True,
            "mismatches": [],
            "detail": f"unexpected manifest schema_version={recorded.get('schema_version')!r}",
        }
    current = build_stage9_evidence_manifest(campaign_root)
    mismatches: list[str] = []
    recorded_evidence = recorded.get("evidence") or {}
    current_evidence = current.get("evidence") or {}
    for evidence_class, fields in _CONSISTENCY_FIELDS.items():
        recorded_class = recorded_evidence.get(evidence_class) or {}
        current_class = current_evidence.get(evidence_class) or {}
        if not current_class.get("present"):
            # Nothing currently on disk for this evidence class -- only a
            # mismatch if the manifest previously claimed it was present.
            if recorded_class.get("present"):
                mismatches.append(f"{evidence_class}: recorded as present in manifest but missing from current campaign_root")
            continue
        for field in fields:
            recorded_value = recorded_class.get(field)
            current_value = current_class.get(field)
            if recorded_value != current_value:
                mismatches.append(f"{evidence_class}.{field}: manifest={recorded_value!r} current={current_value!r}")
    ok = not mismatches
    return {
        "passed": ok,
        "manifest_present": True,
        "mismatches": mismatches,
        "world_versions": current.get("world_versions"),
        "detail": "" if ok else f"{len(mismatches)} field mismatch(es) between manifest and current report files: {mismatches}",
    }
