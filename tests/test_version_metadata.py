from importlib.metadata import PackageNotFoundError

from agentacct import api as api_module
from agentacct import cli as cli_module
from agentacct import mcp as mcp_module
from agentacct import version as version_info


def test_package_version_reads_agentacct_distribution(monkeypatch) -> None:
    requested: list[str] = []

    def fake_distribution_version(name: str) -> str:
        requested.append(name)
        return "9.8.7"

    monkeypatch.setattr(version_info, "_distribution_version", fake_distribution_version)

    assert version_info.package_version() == "9.8.7"
    assert requested == ["agentacct"]


def test_package_version_has_explicit_bare_checkout_fallback(monkeypatch) -> None:
    def missing_distribution(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(version_info, "_distribution_version", missing_distribution)

    assert version_info.package_version() == "0.0.0+source"


def test_public_runtime_metadata_shares_package_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(version_info, "package_version", lambda: "9.8.7")

    app = api_module.create_local_api_app(store_dir=tmp_path / "state")
    initialized = mcp_module.build_initialize_result({})

    assert cli_module._package_version() == "9.8.7"
    assert app.version == "9.8.7"
    assert app.openapi()["info"]["version"] == "9.8.7"
    assert initialized["serverInfo"] == {"name": "agentacct", "version": "9.8.7"}
