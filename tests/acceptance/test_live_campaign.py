"""Live acceptance: runs only against a real campaign bundle.

Usage:
    company-twin campaign --s0-limit 8 --s1-k 3        # on a machine with OPENROUTER_API_KEY
    COMPANY_TWIN_ACCEPT_ROOT=runs/design_campaign_... pytest tests/acceptance -q

Without the env var this test SKIPS (it must never be weakened to pass on
fake or scripted bundles; that is the whole point of the gates).
"""
import os
from pathlib import Path

import pytest

from company_twin.acceptance import run_acceptance
from company_twin.corpus import Corpus
from company_twin.design_loader import load_design


def test_live_campaign_passes_all_gates() -> None:
    root = os.environ.get("COMPANY_TWIN_ACCEPT_ROOT")
    if not root:
        pytest.skip("set COMPANY_TWIN_ACCEPT_ROOT to a live campaign root to run acceptance")
    campaign_root = Path(root).resolve()
    assert campaign_root.exists(), f"campaign root not found: {campaign_root}"
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)
    report = run_acceptance(campaign_root=campaign_root, design=design, corpus=corpus)
    failed = [b for b in report["bundles"] if not b["passed"]] + [g for g in report["campaign_gates"] if not g["passed"]]
    assert report["passed"], f"acceptance failed: {failed[:5]}"
