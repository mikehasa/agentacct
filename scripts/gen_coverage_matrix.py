#!/usr/bin/env python3
"""Regenerate docs/coverage-matrix.md from the capability manifest.

The matrix is the report's "surface the coverage close to the overview" ask,
rendered straight from ``src/agentacct/agent_capabilities.py`` so it can never
drift from the code that is the source of truth. Run after any manifest change:

    PYTHONPATH=src <venv>/python scripts/gen_coverage_matrix.py

``tests/test_docs_generated.py`` fails CI if the committed doc drifts from this.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentacct.agent_capabilities import render_capability_matrix_markdown  # noqa: E402

OUT = REPO_ROOT / "docs" / "coverage-matrix.md"


def main() -> None:
    OUT.write_text(render_capability_matrix_markdown(), encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
