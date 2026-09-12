"""Generated docs must stay in sync with the code that is their source of truth.

- docs/coverage-matrix.md is rendered from the capability manifest.
- docs/examples/*.md are rendered from real Work Receipts by the example generator.

If either drifts, CI fails here with the exact regen command — the same
consistency-contract pattern INSTALL.md uses. This is what lets the docs claim
to reflect the product: they ARE the product's own output, regenerated and
compared, not hand-written prose that can quietly fall out of date.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agentacct.agent_capabilities import render_capability_matrix_markdown  # noqa: E402

import gen_worked_examples  # noqa: E402


def test_coverage_matrix_doc_is_in_sync() -> None:
    committed = (REPO_ROOT / "docs" / "coverage-matrix.md").read_text(encoding="utf-8")
    assert committed == render_capability_matrix_markdown(), (
        "docs/coverage-matrix.md drifted from the manifest — run `python scripts/gen_coverage_matrix.py`"
    )


def test_worked_example_docs_are_in_sync() -> None:
    for rel, content in gen_worked_examples.render_worked_examples().items():
        committed = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert committed == content, (
            f"{rel} drifted from the receipt engine — run `python scripts/gen_worked_examples.py`"
        )


def test_worked_examples_stay_honest_about_synthetic_data() -> None:
    # The examples use invented data; every one must say so, and must never claim
    # a billed cost or an independently-verified check it did not seed.
    docs = gen_worked_examples.render_worked_examples()
    for rel, content in docs.items():
        lower = content.lower()
        assert "synthetic" in lower, f"{rel} must label its data synthetic"
        assert "estimate" in lower, f"{rel} must call its cost an estimate"
        assert "invoice" not in lower or "never billed" in lower or "not provider invoices" in lower, (
            f"{rel} must not imply a billed cost"
        )
