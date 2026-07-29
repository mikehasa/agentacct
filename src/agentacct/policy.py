from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# frozen: the ".agent-sentinel" store/config dir name predates the Agent
# agentacct rename; historical stores/files carry it forever and fresh init
# keeps writing it (store_resolution probes exactly this path).
DEFAULT_POLICY_FILE = Path(".agent-sentinel/policy.yaml")

DEFAULT_POLICY_YAML = """# agentacct project policy
# This file is safe to commit if it does not contain secrets.

budgets:
  max_total_usd: 0.05
  max_total_tokens: null
  max_runtime: 30m

checkpoints:
  every_steps: 10
  on_checkpoint: pause

tools:
  bash:
    destructive: block
    network_install: checkpoint
    git_mutation: checkpoint
  file_write:
    default: checkpoint

providers:
  openrouter:
    enabled: true
"""


@dataclass(frozen=True)
class BudgetPolicy:
    max_total_usd: float | None = 0.05
    max_total_tokens: int | None = None
    max_runtime: str | None = "30m"


@dataclass(frozen=True)
class CheckpointPolicy:
    every_steps: int | None = 10
    on_checkpoint: str = "pause"


@dataclass(frozen=True)
class ProjectPolicy:
    budgets: BudgetPolicy = field(default_factory=BudgetPolicy)
    checkpoints: CheckpointPolicy = field(default_factory=CheckpointPolicy)
    tools: dict[str, Any] = field(default_factory=dict)
    providers: dict[str, Any] = field(default_factory=dict)


def default_policy_path(project_dir: Path | str = Path(".")) -> Path:
    return Path(project_dir) / DEFAULT_POLICY_FILE


def write_default_policy(project_dir: Path | str, *, force: bool = False) -> Path:
    path = default_policy_path(project_dir)
    if path.exists() and not force:
        raise FileExistsError(f"policy already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_POLICY_YAML)
    return path


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def load_policy(path: Path | str) -> ProjectPolicy:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        data = {}
    budgets = _as_mapping(data.get("budgets"))
    checkpoints = _as_mapping(data.get("checkpoints"))
    return ProjectPolicy(
        budgets=BudgetPolicy(
            max_total_usd=budgets.get("max_total_usd", 0.05),
            max_total_tokens=budgets.get("max_total_tokens"),
            max_runtime=budgets.get("max_runtime", "30m"),
        ),
        checkpoints=CheckpointPolicy(
            every_steps=checkpoints.get("every_steps", 10),
            on_checkpoint=checkpoints.get("on_checkpoint", "pause"),
        ),
        tools=_as_mapping(data.get("tools")),
        providers=_as_mapping(data.get("providers")),
    )


def validate_policy(policy: ProjectPolicy) -> list[str]:
    errors: list[str] = []
    if policy.budgets.max_total_usd is not None:
        try:
            if float(policy.budgets.max_total_usd) <= 0:
                errors.append("budgets.max_total_usd must be positive")
        except (TypeError, ValueError):
            errors.append("budgets.max_total_usd must be a number")
    if policy.budgets.max_total_tokens is not None:
        try:
            if int(policy.budgets.max_total_tokens) <= 0:
                errors.append("budgets.max_total_tokens must be positive")
        except (TypeError, ValueError):
            errors.append("budgets.max_total_tokens must be an integer")
    if policy.checkpoints.every_steps is not None:
        try:
            if int(policy.checkpoints.every_steps) <= 0:
                errors.append("checkpoints.every_steps must be positive")
        except (TypeError, ValueError):
            errors.append("checkpoints.every_steps must be an integer")
    if policy.checkpoints.on_checkpoint not in {"pause", "report"}:
        errors.append("checkpoints.on_checkpoint must be pause or report")
    return errors


def load_and_validate_policy(path: Path | str) -> tuple[ProjectPolicy, list[str]]:
    policy = load_policy(path)
    return policy, validate_policy(policy)
