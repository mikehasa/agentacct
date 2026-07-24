from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from agent_chronicle.cli import app


runner = CliRunner()


class _Wire:
    def __init__(self, value: dict[str, object], **attributes: object) -> None:
        self.value = value
        for name, attribute in attributes.items():
            setattr(self, name, attribute)

    def to_dict(self) -> dict[str, object]:
        return self.value


def _json_stdout_only(result) -> dict[str, object]:
    assert result.stdout
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_prepare_cutover_cli_emits_typed_receipt(monkeypatch, tmp_path: Path) -> None:
    import agent_chronicle.canonical.cutover as cutover

    candidate = tmp_path / "candidate.sqlite3"
    report = tmp_path / "parity-report.json"
    preparation = _Wire(
        {"candidate_sha256": "a" * 64},
        candidate_store_uuid="store-uuid",
        canonical_sequence=41,
        candidate_sha256="a" * 64,
        adapter_rows_to_normalize=3,
    )
    monkeypatch.setattr(cutover, "prepare_cutover", lambda *_args: preparation)

    result = runner.invoke(
        app,
        [
            "canonical",
            "prepare-cutover",
            "--candidate",
            str(candidate),
            "--parity-report",
            str(report),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _json_stdout_only(result) == {
        "status": "prepared",
        "preparation": {"candidate_sha256": "a" * 64},
    }


def test_prepare_cutover_cli_json_refusal_uses_stdout_only(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    monkeypatch.setattr(
        cutover,
        "prepare_cutover",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad parity report")),
    )
    result = runner.invoke(
        app,
        [
            "canonical",
            "prepare-cutover",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity.json"),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert _json_stdout_only(result)["status"] == "refused"


def test_prepare_cutover_cli_human_refusal_uses_stderr(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    monkeypatch.setattr(
        cutover,
        "prepare_cutover",
        lambda *_args: (_ for _ in ()).throw(ValueError("bad parity report")),
    )
    result = runner.invoke(
        app,
        [
            "canonical",
            "prepare-cutover",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity.json"),
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "canonical cutover preparation refused" in result.stderr


def test_promote_cli_requires_explicit_writers_stopped_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    def unexpected(*_args, **_kwargs):
        raise AssertionError("promotion must not inspect evidence without confirmation")

    monkeypatch.setattr(cutover, "prepare_cutover", unexpected)
    result = runner.invoke(
        app,
        [
            "canonical",
            "promote",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity-report.json"),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--store-dir",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = _json_stdout_only(result)
    assert payload["status"] == "refused"
    assert "--confirm-writers-stopped is required" in payload["error"]


def test_promote_cli_wires_durable_receipt_and_result(monkeypatch, tmp_path: Path) -> None:
    import agent_chronicle.canonical.cutover as cutover

    candidate = tmp_path / "candidate.sqlite3"
    report = tmp_path / "parity-report.json"
    receipt_path = tmp_path / "promotion-receipt.json"
    preparation = _Wire({"prepared": True})
    promotion = _Wire({"receipt": {"operation_id": "op-1"}, "verification": {"ok": True}})
    observed: dict[str, object] = {}

    monkeypatch.setattr(cutover, "prepare_cutover", lambda *_args: preparation)

    def promote(prepared, store_root, *, receipt_path):
        observed.update(
            preparation=prepared,
            store_root=store_root,
            receipt_path=receipt_path,
        )
        Path(receipt_path).write_text("{}", encoding="utf-8")
        return promotion

    monkeypatch.setattr(cutover, "promote_candidate", promote)
    result = runner.invoke(
        app,
        [
            "canonical",
            "promote",
            "--candidate",
            str(candidate),
            "--parity-report",
            str(report),
            "--receipt",
            str(receipt_path),
            "--store-dir",
            str(tmp_path),
            "--confirm-writers-stopped",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json_stdout_only(result)
    assert payload["status"] == "promoted"
    assert payload["receipt_path"] == str(receipt_path)
    assert payload["verification"] == {"ok": True}
    assert observed == {
        "preparation": preparation,
        "store_root": tmp_path,
        "receipt_path": receipt_path,
    }


def test_promote_cli_reports_installed_state_and_emergency_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    requested = tmp_path / "promotion-receipt.json"
    fallback = tmp_path / ".chronicle.sqlite3.promotion-receipt-op.json"
    promotion_receipt = _Wire({"operation_id": "op-1"})
    monkeypatch.setattr(cutover, "prepare_cutover", lambda *_args: object())

    def fail_after_install(*_args, **_kwargs):
        raise cutover.CutoverReceiptPersistenceError(
            promotion_receipt,
            requested,
            OSError("receipt disk unavailable"),
            fallback_receipt_path=fallback,
        )

    monkeypatch.setattr(cutover, "promote_candidate", fail_after_install)
    result = runner.invoke(
        app,
        [
            "canonical",
            "promote",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity-report.json"),
            "--receipt",
            str(requested),
            "--store-dir",
            str(tmp_path),
            "--confirm-writers-stopped",
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = _json_stdout_only(result)
    assert payload["status"] == "promoted_receipt_persistence_failed"
    assert payload["fallback_receipt_path"] == str(fallback)
    assert payload["emergency_receipt"] == {"operation_id": "op-1"}


def test_promote_cli_reports_ambiguous_replace_on_stdout_only(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    fallback = tmp_path / ".chronicle.sqlite3.promotion-receipt-op.json"
    promotion_receipt = _Wire({"operation_id": "op-ambiguous"})
    monkeypatch.setattr(cutover, "prepare_cutover", lambda *_args: object())

    def fail_ambiguously(*_args, **_kwargs):
        raise cutover.CutoverPostReplaceError(
            promotion_receipt,
            OSError("live inode could not be identified"),
            fallback_receipt_path=fallback,
            replacement_state="ambiguous",
        )

    monkeypatch.setattr(cutover, "promote_candidate", fail_ambiguously)
    result = runner.invoke(
        app,
        [
            "canonical",
            "promote",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity-report.json"),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--store-dir",
            str(tmp_path),
            "--confirm-writers-stopped",
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = _json_stdout_only(result)
    assert payload["status"] == "promotion_replace_ambiguous"
    assert payload["replacement_state"] == "ambiguous"
    assert payload["emergency_receipt"] == {"operation_id": "op-ambiguous"}

    human_result = runner.invoke(
        app,
        [
            "canonical",
            "promote",
            "--candidate",
            str(tmp_path / "candidate.sqlite3"),
            "--parity-report",
            str(tmp_path / "parity-report.json"),
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--store-dir",
            str(tmp_path),
            "--confirm-writers-stopped",
        ],
    )

    assert human_result.exit_code == 2
    assert human_result.stdout == ""
    assert "could not determine whether the live store was replaced" in human_result.stderr


def test_verify_cutover_cli_exits_two_on_verification_blocker(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    receipt_path = tmp_path / "promotion-receipt.json"
    verification = _Wire(
        {"ok": False, "errors": ["digest changed"]},
        ok=False,
        errors=("digest changed",),
    )
    monkeypatch.setattr(cutover, "load_promotion_receipt", lambda _path: object())
    monkeypatch.setattr(cutover, "verify_promotion", lambda _receipt: verification)

    result = runner.invoke(
        app,
        [
            "canonical",
            "verify-cutover",
            "--receipt",
            str(receipt_path),
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = _json_stdout_only(result)
    assert payload["status"] == "blocked"
    assert payload["verification"]["errors"] == ["digest changed"]


def test_rollback_cli_requires_confirmation_before_loading_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    def unexpected(_path):
        raise AssertionError("receipt must not be opened without confirmation")

    monkeypatch.setattr(cutover, "load_promotion_receipt", unexpected)
    result = runner.invoke(
        app,
        [
            "canonical",
            "rollback",
            "--receipt",
            str(tmp_path / "promotion-receipt.json"),
            "--json",
        ],
    )

    assert result.exit_code == 2
    assert _json_stdout_only(result)["status"] == "refused"


def test_rollback_cli_reports_installed_verification_failure(
    monkeypatch, tmp_path: Path
) -> None:
    import agent_chronicle.canonical.cutover as cutover

    rollback_receipt = _Wire({"operation_id": "rollback-op"})
    verification = _Wire(
        {"ok": False, "errors": ["shadow digest mismatch"]},
        ok=False,
        errors=("shadow digest mismatch",),
    )
    monkeypatch.setattr(cutover, "load_promotion_receipt", lambda _path: object())

    def fail_after_replace(_receipt):
        raise cutover.RollbackPostReplaceError(
            rollback_receipt,
            RuntimeError("post-replace verification failed"),
            replacement_state="installed",
            verification=verification,
        )

    monkeypatch.setattr(cutover, "rollback_promotion", fail_after_replace)
    result = runner.invoke(
        app,
        [
            "canonical",
            "rollback",
            "--receipt",
            str(tmp_path / "promotion-receipt.json"),
            "--confirm-writers-stopped",
            "--json",
        ],
    )

    assert result.exit_code == 2
    payload = _json_stdout_only(result)
    assert payload["status"] == "rolled_back_verification_failed"
    assert payload["replacement_state"] == "installed"
    assert payload["rollback_receipt"] == {"operation_id": "rollback-op"}
    assert payload["verification"]["ok"] is False


def test_read_canary_cli_propagates_structured_blockers(monkeypatch) -> None:
    import agent_chronicle.canonical.read_canary as canary

    blocker = SimpleNamespace(
        code="canonical_read_disabled",
        surface=None,
        message="canonical read flag is not enabled",
    )
    result_value = _Wire(
        {"ready": False, "blockers": [{"code": blocker.code}]},
        ready=False,
        blockers=(blocker,),
        probed_surfaces=(),
        skipped_surfaces=(),
    )
    monkeypatch.setattr(canary, "verify_read_canary", lambda **_kwargs: result_value)

    result = runner.invoke(
        app,
        ["canonical", "verify-read-canary", "--json"],
    )

    assert result.exit_code == 2
    assert _json_stdout_only(result) == {
        "ready": False,
        "blockers": [{"code": "canonical_read_disabled"}],
    }
