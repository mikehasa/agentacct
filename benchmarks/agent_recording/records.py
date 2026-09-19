"""What an agent actually recorded, read back out of the sandbox's store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scenarios import TERMINAL


@dataclass
class Section:
    section_id: str
    title: str = ""
    goal: str = ""
    statuses: list[str] = field(default_factory=list)
    summary: str = ""
    blocker: str = ""
    next_step: str = ""
    rest_of_work: str = ""
    kind: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def final_status(self) -> str:
        return self.statuses[-1] if self.statuses else ""


@dataclass
class Check:
    name: str
    result: str
    command: str = ""
    exit_code: int | None = None
    summary: str = ""
    rest_of_work: str = ""


@dataclass
class Record:
    sections: list[Section] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.sections and not self.checks

    @property
    def goal(self) -> str:
        return next((section.goal for section in self.sections if section.goal), "")

    @property
    def terminal_statuses(self) -> set[str]:
        return {section.final_status for section in self.sections} & TERMINAL

    @property
    def files(self) -> set[str]:
        return {path for section in self.sections for path in section.files}

    def render(self) -> str:
        """The record as plain labelled text, in the order the record page reads:
        what it was for, the verdict, the prose, then the evidence."""

        if self.empty:
            return "(the agent recorded nothing)"
        lines: list[str] = []
        for section in self.sections:
            lines.append(f"TASK STEP: {section.title or '(untitled)'}")
            if section.goal:
                lines.append(f"  goal: {section.goal}")
            lines.append(f"  status: {' -> '.join(section.statuses) or '(none)'}")
            for label, value in (
                ("summary", section.summary), ("blocker", section.blocker),
                ("next step", section.next_step), ("rest of the work", section.rest_of_work),
            ):
                if value:
                    lines.append(f"  {label}: {value}")
            if section.files:
                lines.append(f"  files: {', '.join(section.files)}")
        for check in self.checks:
            code = "" if check.exit_code is None else f" (exit {check.exit_code})"
            lines.append(f"CHECK: {check.name or '(unnamed)'} -- {check.result}{code}")
            if check.command:
                lines.append(f"  command: {check.command}")
            if check.summary:
                lines.append(f"  summary: {check.summary}")
            if check.rest_of_work:
                lines.append(f"  rest of the work: {check.rest_of_work}")
        return "\n".join(lines)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def from_events(events: list[dict[str, Any]]) -> Record:
    """Fold stored events into sections and checks, oldest first. Later prose
    replaces earlier prose on the same section; a goal is kept from its first
    appearance, matching how the ledger itself treats the two."""

    record = Record()
    by_id: dict[str, Section] = {}
    for event in sorted(events, key=lambda row: row.get("created_at") or 0):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_type = str(event.get("event_type") or "")
        if event_type.startswith("section_"):
            section_id = _text(metadata.get("section_id")) or event_type
            section = by_id.get(section_id)
            if section is None:
                section = by_id[section_id] = Section(section_id)
                record.sections.append(section)
            section.statuses.append(_text(metadata.get("section_status")) or event_type.removeprefix("section_"))
            section.title = _text(metadata.get("section_title")) or section.title
            section.goal = section.goal or _text(metadata.get("task_goal"))
            for name in ("summary", "blocker", "next_step", "rest_of_work", "kind"):
                setattr(section, name, _text(metadata.get(name)) or getattr(section, name))
            for path in metadata.get("files") or []:
                if isinstance(path, str) and path not in section.files:
                    section.files.append(path)
        elif event_type == "machine_check":
            code = metadata.get("exit_code")
            record.checks.append(Check(
                name=_text(metadata.get("name")), result=_text(metadata.get("result")).lower(),
                command=_text(metadata.get("command")),
                exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                summary=_text(metadata.get("summary")), rest_of_work=_text(metadata.get("rest_of_work")),
            ))
    return record


def read_store(store: Path) -> Record:
    from agentacct.service import SentinelService

    return from_events(SentinelService(store).list_events(limit=500))
