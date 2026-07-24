from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "evidence" / "golden_scenarios.json"


def test_golden_scenarios_are_unique_and_machine_readable() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "golden-scenarios-v1"
    assert payload["invariants"] == {
        "false_exact": 0,
        "metadata_only": True,
        "overlapping_cost_claims_are_summed": False,
        "missing_cost_is_zero": False,
        "mcp_required_for_session_existence": False,
    }
    scenarios = payload["scenarios"]
    scenario_ids = [scenario["id"] for scenario in scenarios]
    assert len(scenarios) >= 20
    assert len(scenario_ids) == len(set(scenario_ids))
    assert all(scenario["input"].strip() for scenario in scenarios)
    assert all(scenario["expected"].strip() for scenario in scenarios)


def test_license_bom_pins_audited_upstream_commits() -> None:
    bom = (REPO_ROOT / "docs" / "integration-license-bom.md").read_text(encoding="utf-8")

    assert "8adf21c8f952c0768fd5ff85d853798bb3c028f3" in bom
    assert "7cd6662805fbd525f2f418ecf465a247b924af70" in bom
    assert "c36f1a4afd91e4ddf0e5c7224b288ce722c7404f" in bom
    assert "Copied/vendored source" in bom
    assert "None" in bom


def test_architecture_keeps_mcp_first_class_but_non_mandatory() -> None:
    architecture = (
        REPO_ROOT / "docs" / "multi-source-evidence-architecture.md"
    ).read_text(encoding="utf-8")

    assert "MCP remains a first-class, high-value semantic source" in architecture
    assert "MCP absence never means that work or usage did not exist" in architecture
    assert "v1 `events.jsonl` remains unchanged" in architecture
