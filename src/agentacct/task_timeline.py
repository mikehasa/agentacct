"""Shared task timeline records and bounded, immutable history-page snapshots.

The receipt, native UI and text renderers use the same event projection. Clients
own viewport/selection state; they do not infer event identity or relationships.
"""
from __future__ import annotations

from .display_budget import display_label_from_text
from .display_vocabulary import (
    ARTIFACT_PATH_NOT_SHOWN_TEXT,
    ARTIFACT_URL_NOT_SHOWN_TEXT,
    COMMAND_NOT_SHOWN_TEXT,
    SALIENCE_BLOCKER,
    SALIENCE_CHECK_COULD_NOT_RUN,
    SALIENCE_COMPLETED_UNCHECKED,
    SALIENCE_CONTROL_RECORD,
    SALIENCE_CURRENT_FAILURE,
    SALIENCE_LARGEST_FILE_SET,
    SALIENCE_LEFT_IN_PROGRESS,
    SALIENCE_OWNS_FAILED_CHECK,
    SALIENCE_RECOVERY_RUN,
    TIMELINE_BEAT_TIME_NOTE,
    command_state_text,
    FAILED_CHECK_RESULTS,
    NOT_RUN_CHECK_RESULTS,
    check_event_status_label,
    timeline_beat_title,
    timeline_salience,
    work_status_label,
    step_status_label,
    timeline_lane_label,
    check_result_note,
    check_result_tone,
    evidence_grade_label,
    source_label,
)
from .semantic_rules import TERMINAL_STATUSES
from .task_outcome import GRADE_CLAIMED, step_evidence_grade, step_is_checkable
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import math
import threading
import time
from typing import Any
import uuid

SCHEMA = "agentacct.task-timeline.v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def valid_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number < 253_402_300_800 else None


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _files(value: Any) -> list[str]:
    return sorted({_text(path) for path in value if isinstance(path, str) and _text(path)}) if isinstance(value, list) else []


def check_source(check: Mapping[str, Any]) -> str:
    """Trust the ledger's source type, never an agent-authored source name."""
    source_type = _text(check.get("source_type")).lower()
    if source_type == "client_hook":
        return "hook"
    if source_type in {"ci", "external", "provider"}:
        return "ci"
    return "mcp"


def task_checks(task: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Keep historical evidence, deduplicating only supplied immutable IDs."""
    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    sources = [task.get("task_evidence_events")]
    sources.extend(item.get("evidence_events") for item in _rows(task.get("work_items")))
    for source in sources:
        for row in _rows(source):
            key = _text(row.get("event_id"))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
    return sorted(rows, key=lambda row: valid_time(row.get("created_at") or row.get("occurred_at")) or 0)


def _step_is_checkable(item: Mapping[str, Any]) -> bool:
    """Whether this step owes a check at all (a docs or review step does not)."""

    raw = item.get("current_check_events") if isinstance(item.get("current_check_events"), list) else item.get("evidence_events")
    return step_is_checkable(item, _rows(raw))


def _grade_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """One step's evidence grade, its label and the reducer's reason sentence."""

    grade = step_evidence_grade(item)
    return {"evidence_grade": grade.get("grade"),
            "evidence_grade_label": evidence_grade_label(grade.get("grade"), checkable=_step_is_checkable(item)),
            "evidence_grade_reason": grade.get("reason")}


#: The recorded open states. A section left in one of these never reported how
#: it ended, which is the one thing a reviewer cannot reconstruct from the rest
#: of the record. An item with no status at all is an absence, not an open
#: section, so it is deliberately not here.
_OPEN_SECTION_STATUSES = frozenset({"started", "checkpoint"})


def largest_recorded_file_set(task: Mapping[str, Any]) -> int:
    """How many files the Task's biggest section touched.

    Salience has to come from something that VARIES within the Task being read.
    "Touched more files than any other section here" does; "touched files" does
    not. A Task whose biggest section named one or two paths has no biggest
    section worth pulling forward, so the threshold is three.
    """

    counts = [len(_files(item.get("files"))) for item in _rows(task.get("work_items"))]
    largest = max(counts, default=0)
    return largest if largest >= 3 and len(counts) > 1 else 0


def _work_salience_keys(item: Mapping[str, Any], *, status: str, grade: Any, largest_files: int) -> list[str]:
    """Why a reviewer would open THIS section rather than the one beside it."""

    keys: list[str] = []
    if _text(item.get("blocker")):
        keys.append(SALIENCE_BLOCKER)
    if any(
        _text(check.get("result")).lower() in FAILED_CHECK_RESULTS
        and _text(check.get("supersession_state")) != "superseded"
        for check in _rows(item.get("evidence_events"))
    ):
        keys.append(SALIENCE_OWNS_FAILED_CHECK)
    if status in _OPEN_SECTION_STATUSES:
        keys.append(SALIENCE_LEFT_IN_PROGRESS)
    # A completion nobody checked is the gap between the two axes, on one row.
    # Only a step that OWES a check counts: a docs or review step reported
    # complete with no check is not check-relevant, not unproven.
    if status == "completed" and _text(grade) == GRADE_CLAIMED and _step_is_checkable(item):
        keys.append(SALIENCE_COMPLETED_UNCHECKED)
    if largest_files and len(_files(item.get("files"))) == largest_files:
        keys.append(SALIENCE_LARGEST_FILE_SET)
    return keys


#: The timeline kind for a recorded progress note. It is deliberately NOT
#: ``work``: a beat is narration under a section, and every count that reads
#: this list (steps, coverage denominators, the check tally) is taken from the
#: work items and the check events, never from the row count.
BEAT_KIND = "beat"


def _beats(item: Mapping[str, Any], section: Mapping[str, Any], session: Mapping[str, Any],
           identity) -> list[dict[str, Any]]:
    """One ordered record per progress note the section recorded while open.

    The recording contract asks agents to send `checkpoint` updates instead of
    one giant section, and the ledger keeps one summary per section — so the
    prose those updates carried was stored and then unreachable in every
    projection. Each note becomes its own beat, in record order, under the
    section that wrote it. The section keeps its terminal summary as its
    headline; a beat never restates it.

    A beat is never salient on its own. Its section carries the salience, and a
    beat that also claimed it would make one section shout twice in the very
    list this change exists to make legible.
    """

    rows: list[dict[str, Any]] = []
    for note in _rows(item.get("progress_notes")):
        prose = _text(note.get("summary"))
        if not prose:
            continue
        at = valid_time(note.get("at"))
        note_id = _text(note.get("event_id"))
        status = _text(note.get("status")) or "checkpoint"
        rows.append({"id": identity(BEAT_KIND + ":" + (note_id or _hash([section["id"], prose]))),
                     "event_id": note_id or None, "kind": BEAT_KIND, "occurred_at": at, "started_at": at,
                     "updated_at": None, "terminal_status_at": None,
                     "time_note": TIMELINE_BEAT_TIME_NOTE if at else "Source time unavailable",
                     "time_warning": None, "lane": section["lane"], "lane_label": section["lane_label"],
                     "title": timeline_beat_title(display_label_from_text(prose)),
                     "status": status, "status_label": step_status_label(status),
                     "source": section["source"], "source_label": source_label("mcp"),
                     "confidence": "claimed", "salience": None, "salience_reason": None,
                     "salience_keys": [], "important": False, "scope": section["scope"],
                     "summary": prose, "files": [], "resolution": None,
                     "section_kind": section.get("section_kind"), "next_step": None,
                     "section_record_id": section["id"], "section_record_ids": [section["id"]],
                     "section_title": section["title"], "identity_note": None, **session})
    return rows


def _revision(check: Mapping[str, Any]) -> dict[str, Any] | None:
    """A check's captured git revision: the ledger's flat git_* fields, or an
    already-shaped ``revision`` object."""

    if isinstance(check.get("revision"), Mapping):
        return check["revision"]
    if not (_text(check.get("git_commit")) or _text(check.get("git_revision_basis"))):
        return None
    # The BASIS travels with the revision: it is what decides whether the label
    # may say "ran at" or must say "HEAD when recorded".
    return {"commit": _text(check.get("git_commit")) or None, "branch": _text(check.get("git_branch")) or None,
            "dirty": check.get("git_dirty") if isinstance(check.get("git_dirty"), bool) else None,
            "basis": _text(check.get("git_revision_basis")) or "unavailable"}


def session_display_titles(task: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    """Use the first recorded section when a supporting session has no title."""
    titles: dict[tuple[str, str], str] = {}
    for item in _rows(task.get("work_items")):
        key = (_text(item.get("client") or item.get("reporting_source")), _text(item.get("client_session_id")))
        title = _text(item.get("title") or item.get("objective")) or display_label_from_text(item.get("summary"))
        if all(key) and title:
            titles.setdefault(key, title)
    for session in _rows(task.get("sessions")):
        key = (_text(session.get("client")), _text(session.get("client_session_id")))
        title = _text(session.get("client_session_title") or session.get("title"))
        if all(key) and title:
            titles[key] = title
    return titles


def build_timeline_events(task: Mapping[str, Any], checks: Sequence[Mapping[str, Any]] | None = None,
                          control: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Project already joined task evidence. Only a supplied event ID deduplicates checks."""
    primary = task.get("primary_root") if isinstance(task.get("primary_root"), Mapping) else {}
    primary_key = (_text(primary.get("client")), _text(primary.get("client_session_id")))
    titles = session_display_titles(task)
    events: list[dict[str, Any]] = []
    sections_by_event: dict[str, list[dict[str, Any]]] = {}
    occurrences: dict[str, int] = {}

    def identity(base: str) -> str:
        occurrences[base] = occurrences.get(base, 0) + 1
        return base if occurrences[base] == 1 else f"{base}#{occurrences[base]}"

    def session_fields(key: tuple[str, str]) -> dict[str, Any]:
        known = bool(key[0] and key[1])
        role = "Root" if key == primary_key else "Supporting"
        title = titles.get(key, "")
        return {"client": key[0] or None, "client_session_id": key[1] or None,
                "session_key": f"{key[0]}::{key[1]}" if known else None,
                "session_title": title or (f"{key[0]} · {key[1][:16]}" if known else "Task evidence"),
                "lineage": f"{role} session · {key[1]}" if known else "Session attribution unavailable"}

    largest_files = largest_recorded_file_set(task)
    for item in _rows(task.get("work_items")):
        key = (_text(item.get("client") or item.get("reporting_source")), _text(item.get("client_session_id")))
        stable = _text(item.get("work_id") or item.get("section_id"))
        record_id = identity("work:" + _hash([*key, stable] if stable else [*key, dict(item)]))
        first, last = valid_time(item.get("started_at")), valid_time(item.get("updated_at"))
        warning = "The recorded update precedes the start. Timing is inconsistent." if first and last and last < first else None
        end = last if first and last and last > first else None
        source = _text(item.get("reporting_source") or item.get("client"))
        status = _text(item.get("latest_status"))
        # WHEN the agent reported a terminal status (completed, blocked, handed
        # off): its latest update, never the section's start. Renderers mark
        # that moment instead of implying the stop happened at the beginning.
        # Python owns which statuses are terminal, so no surface re-decides it.
        terminal_at = (end or first) if status in TERMINAL_STATUSES else None
        grade_fields = _grade_fields(item)
        # SALIENCE IS A PAYLOAD FACT. `important` used to be `bool(blocker)`,
        # true for 5 sections in the whole installed ledger, so the mark said
        # nothing about any particular Task. It is now the strongest of several
        # per-section reasons and travels with the sentence that explains it.
        salience = timeline_salience(
            _work_salience_keys(
                item,
                status=status,
                grade=grade_fields.get("evidence_grade"),
                largest_files=largest_files,
            )
        )
        event = {"id": record_id, "event_id": None, "kind": "work", "occurred_at": first or last,
                 "started_at": first or last, "updated_at": end,
                 "terminal_status_at": terminal_at,
                 "time_note": "Source time unavailable" if not (first or last) else "Recorded section start → latest update; not execution duration" if end else "Recorded section point; duration unavailable",
                 "time_warning": warning, "lane": "primary" if key == primary_key else "supporting",
                 "lane_label": timeline_lane_label("primary" if key == primary_key else "supporting"),
                 # A card title is a LABEL, not a paragraph. Falling back to the
                 # summary handed a 1,200-character paragraph to a card that
                 # renders two lines of 14 pt text in a 200x80 pt box, so the
                 # reader saw an arbitrary middle slice of a sentence. The fallback
                 # now reduces the prose to a statement at the label budget; the
                 # full summary still travels in its own field.
                 "title": _text(item.get("title"))
                 or display_label_from_text(item.get("summary"))
                 or "Recorded work",
                 "status": status or "recorded",
                 # The agent-asserted step status, qualified as its report.
                 "status_label": step_status_label(item.get("latest_status")), "source": source,
                 "source_label": source_label("mcp"), "confidence": _text(item.get("join_confidence")) or "claimed",
                 **salience, "scope": stable or None,
                 "summary": _text(item.get("summary")) or None, "files": _files(item.get("files")),
                 "resolution": f"Reported blocker: {_text(item['blocker'])}" if item.get("blocker") else None,
                 # The step's declared kind travels as section_kind; ``kind`` stays
                 # the event kind ("work") every renderer switches on.
                 "section_kind": _text(item.get("kind")) or None,
                 # The recorded continuation point, verbatim (None when none).
                 "next_step": _text(item.get("next_step")) or None,
                 "identity_note": None if stable else "No stable section identity; content identity is used.",
                 **grade_fields,
                 **session_fields(key)}
        events.append(event)
        events.extend(_beats(item, event, session_fields(key), identity))
        for check in _rows(item.get("evidence_events")):
            if event_id := _text(check.get("event_id")):
                sections_by_event.setdefault(event_id, []).append(event)

    from .finding_disposition import finding_target_digest
    from .receipt import check_display_name, check_recorded_summary, check_title, revision_label
    episodes = {_text(row.get("target_digest")): row for row in _rows(task.get("finding_episodes"))}
    check_rows = list(task_checks(task) if checks is None else checks)
    # Which run of each check is the CURRENT one. `supersession_state` only
    # demotes a failure that a later PASS retired, so a check re-run while it is
    # being narrowed down (three failures, then one, then a fix) left every one
    # of those runs claiming to be a standing failure -- three coral marks in
    # the timeline for a single check, while the receipt's findings, which read
    # one row per identity, correctly showed one. A run that a later run of the
    # SAME identity replaced is history: it keeps its Failed label (that run did
    # fail) but stops asserting that nothing has replaced it.
    #
    # Only a STABLE identity may group runs. The type fallback ("type:test") is
    # shared by every unrelated test check in the Task, so grouping on it would
    # silence real failures.
    latest_run_by_identity: dict[str, str] = {}
    for row in sorted(check_rows, key=lambda r: (valid_time(r.get("created_at")) or 0.0, _text(r.get("event_id")))):
        if row.get("check_identity_stable") is not True:
            continue
        identity_key = _text(row.get("check_identity"))
        run_id = _text(row.get("event_id"))
        if identity_key and run_id:
            latest_run_by_identity[identity_key] = run_id
    seen: set[str] = set()
    for check in check_rows:
        event_id = _text(check.get("event_id"))
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        record_id = identity("event:" + event_id if event_id else "anonymous-check:" + _hash(dict(check)))
        parents = list({row["id"]: row for row in sections_by_event.get(event_id, [])}.values())
        parent = parents[0] if len(parents) == 1 else None
        raw_key = (_text(check.get("client")), _text(check.get("client_session_id")))
        parent_key = (parent["client"], parent["client_session_id"]) if parent else ("", "")
        conflict = any(raw and raw != inherited
                       for candidate in parents
                       for raw, inherited in zip(raw_key, (candidate["client"], candidate["client_session_id"])))
        if conflict:
            parents, parent = [], None
        key = raw_key if all(raw_key) else parent_key if parent else ("", "")
        source = check_source(check)
        at = valid_time(check.get("created_at") or check.get("occurred_at"))
        episode = episodes.get(str(finding_target_digest(check) or ""), {})
        disposition = _text(episode.get("disposition_state")) if episode.get("attention_open") is False else None
        result = _text(check.get("result")).lower()
        failed = result in FAILED_CHECK_RESULTS
        not_run = result in NOT_RUN_CHECK_RESULTS
        superseded = _text(check.get("supersession_state")) == "superseded"
        identity_key = _text(check.get("check_identity")) if check.get("check_identity_stable") is True else ""
        replaced_by_a_later_run = bool(
            identity_key
            and event_id
            and latest_run_by_identity.get(identity_key, event_id) != event_id
        )
        # The one "still needs you" predicate the receipt's attention block uses:
        # the finding episode's attention_open when surfaced, else an
        # undisposed standing failure. checks_failed stays an evidence count.
        attention_open = (bool(episode.get("attention_open")) if "attention_open" in episode
                          else (_text(episode.get("disposition_state")) or "open") == "open")
        standing = not superseded and not replaced_by_a_later_run and attention_open
        command_redacted = check.get("command_redacted") is True
        command_state = _text(check.get("command_state")) or None
        note = "Conflicting session identity; no section relationship is assumed." if conflict else None
        if not event_id:
            note = "No event ID; identical anonymous records remain separate."
        event = {"id": record_id, "event_id": event_id or None, "kind": "check", "occurred_at": at,
                 "started_at": at, "updated_at": None, "time_note": "Recorded check point; duration unavailable" if at else "Source time unavailable",
                 "lane": "evidence", "lane_label": timeline_lane_label("evidence"), "title": check_title(check),
                 "name": check_display_name(check),
                 "status": result or "unknown",
                 # The shared result words and tone key: renderers map the tone
                 # to a glyph/color and print the label, never the raw key.
                 "status_label": check_event_status_label(result, superseded=superseded),
                 "result_tone": check_result_tone(result),
                 "note_text": check_result_note(result, check.get("exit_code")), "source": source,
                 "source_label": source_label(source), "confidence": "observed",
                 # Every check used to be salient, which made none of them
                 # salient. A standing failure, a check that could not run and
                 # a run that repaired an earlier failure are what a reviewer
                 # opens; a routine pass is the background they are read against.
                 **timeline_salience(
                     ([SALIENCE_CURRENT_FAILURE] if failed and standing else [])
                     + ([SALIENCE_CHECK_COULD_NOT_RUN] if not_run and standing else [])
                     + ([SALIENCE_RECOVERY_RUN] if _text(check.get("supersedes_check_event_id")) else [])
                 ),
                 "scope": _text(check.get("check_identity") or check.get("resolution_scope")) or None,
                 "summary": check_recorded_summary(check), "files": _files(check.get("files")),
                 "section_record_id": parent["id"] if parent else None,
                 "section_record_ids": [row["id"] for row in parents], "section_title": parent["title"] if parent else None,
                 "superseded": superseded,
                 "superseded_by_event_id": _text(check.get("superseded_by_event_id")) or None,
                 # The reciprocal link: the failed run this passing run names as fixed.
                 "supersedes_check_event_id": _text(check.get("supersedes_check_event_id")) or None,
                 "revision_label": revision_label(_revision(check)),
                 "is_current_failure": failed and not superseded and attention_open,
                 # A check that could not run and still needs a look: a named
                 # gap, never counted with the failures above.
                 "is_current_not_run": not_run and not superseded and attention_open,
                 "resolution": _text(check.get("resolution_summary")) or None,
                 "resolution_scope": _text(check.get("resolution_scope")) or None,
                 "identity_note": note, "disposition": disposition or None, **session_fields(key)}
        event["exit_code"] = check.get("exit_code") if type(check.get("exit_code")) is int else None
        for field in ("artifact_ref", "artifact_path", "artifact_url"):
            event[field] = _text(check.get(field)) or None
        for field in ("artifact_path_redacted", "artifact_url_redacted", "command_redacted"):
            event[field] = check.get(field) is True
        # Two different command absences, two different sentences: the agent
        # volunteered the command (stored; the recorded name is shown instead)
        # versus a hook check that only ever held a sha256 digest.
        event["command_state"] = command_state
        event["command_state_text"] = command_state_text(command_state) or (
            COMMAND_NOT_SHOWN_TEXT if command_redacted else None
        )
        event["artifact_path_state_text"] = ARTIFACT_PATH_NOT_SHOWN_TEXT if event["artifact_path_redacted"] else None
        event["artifact_url_state_text"] = ARTIFACT_URL_NOT_SHOWN_TEXT if event["artifact_url_redacted"] else None
        # A source's explicit redaction flag always wins over accidentally retained values.
        if event.get("artifact_path_redacted"):
            event["artifact_path"] = None
        if event.get("artifact_url_redacted"):
            event["artifact_url"] = None
        events.append(event)

    if isinstance(control, Mapping):
        for kind, rows in [("attempt", _rows(control.get("attempts"))), ("control", _rows(control.get("events")))]:
            for row in rows:
                stable = _text(row.get("attempt_id") if kind == "attempt" else row.get("event_id"))
                at = valid_time(row.get("started_at") or row.get("created_at") if kind == "attempt" else row.get("occurred_at"))
                events.append({"id": identity(kind + ":" + (stable or _hash(dict(row)))), "kind": kind,
                               "occurred_at": at, "started_at": at, "lane": "control",
                               "lane_label": timeline_lane_label("control"),
                               "title": "agentacct-owned execution attempt" if kind == "attempt" else _text(row.get("action") or "Control action").replace("_", " ").title(),
                               "status": _text(row.get("execution_state") if kind == "attempt" else row.get("next_state")) or "recorded",
                               "status_label": work_status_label(row.get("execution_state") if kind == "attempt" else row.get("next_state")),
                               "source": "agentacct Control Store", "source_label": "agentacct Control Store", "confidence": "owned",
                               # A control record stays salient: agentacct RAN
                               # it, so it is the only lane that is owned rather
                               # than reported, and it is rare enough that the
                               # mark still distinguishes rows. Unlike before it
                               # now carries the sentence saying why.
                               **timeline_salience([SALIENCE_CONTROL_RECORD]),
                               **session_fields(("", ""))})
    return sorted(events, key=lambda event: (event["occurred_at"] is None, event["occurred_at"] or 0, event["id"]))


class TimelineCursorError(ValueError):
    """A malformed, expired or cross-task cursor must never mix snapshots."""


class TimelineSnapshotCache:
    def __init__(self, *, capacity: int = 8, ttl: float = 120, clock=time.monotonic):
        if capacity < 1 or ttl <= 0:
            raise ValueError("snapshot capacity and lifetime must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._snapshots: OrderedDict[str, tuple[float, str, list[dict[str, Any]]]] = OrderedDict()
        self._lock = threading.Lock()

    def page(self, task_id: str, *, limit: int, events: list[dict[str, Any]] | None = None,
             cursor: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            raise ValueError("timeline limit must be between 1 and 500")
        with self._lock:
            now = self.clock()
            for key in list(self._snapshots):
                if now - self._snapshots[key][0] >= self.ttl:
                    del self._snapshots[key]
            if cursor is None:
                if events is None:
                    raise ValueError("initial timeline page needs events")
                # Reuse an unchanged snapshot so polling clients can keep their
                # assembled history without downloading every older page again.
                snapshot = next((key for key, saved in reversed(self._snapshots.items())
                                 if saved[1] == task_id and saved[2] == events), uuid.uuid4().hex)
                offset = 0
                if snapshot in self._snapshots:
                    self._snapshots[snapshot] = (now, task_id, self._snapshots[snapshot][2])
                    self._snapshots.move_to_end(snapshot)
                else:
                    self._snapshots[snapshot] = (now, task_id, deepcopy(events))
                while len(self._snapshots) > self.capacity:
                    self._snapshots.popitem(last=False)
            else:
                try:
                    snapshot, encoded = cursor.split(":")
                    offset = int(encoded)
                    if str(offset) != encoded or offset < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise TimelineCursorError("Invalid timeline cursor") from None
            saved = self._snapshots.get(snapshot)
            if saved is None or saved[1] != task_id or offset > len(saved[2]):
                raise TimelineCursorError("Timeline history expired; reload the task")
            self._snapshots.move_to_end(snapshot)
            rows = saved[2]
            # Newest page first, preserving chronological order within each page.
            end = len(rows) - offset
            start = max(0, end - limit)
            shown = end - start
            return {"schema_version": SCHEMA, "task_id": task_id, "snapshot_id": snapshot,
                    "events": deepcopy(rows[start:end]), "offset": offset, "shown": shown, "total": len(rows),
                    "truncated": start > 0, "next_cursor": f"{snapshot}:{offset + shown}" if start else None}
