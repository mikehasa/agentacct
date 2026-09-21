#!/usr/bin/env python3
"""Complete test fixtures so they satisfy the recording contract.

The contract now requires fields that most fixtures were written without: a
terminal section carries outcome prose, a section carries a title, a machine
check carries something a reviewer could re-run. Those requirements are right for
agents and incidental for fixtures, so this script adds the missing fields
mechanically instead of asking a human to touch ~200 test bodies.

It is AST-guided on purpose. A regex pass over these files was tried first and
corrupted multi-line dictionaries, because `"section_status": status,` appears in
contexts where the next line is another key, a closing brace, or a call argument.

Usage:
    python3 design-plans/data-quality/tools/complete-test-fixtures.py [--dry-run] [tests/...]

Standard library only, so a bare system ``python3`` runs it; that is deliberate,
because it is the tool you reach for when the suite will not even import.

Safe to re-run: every insertion is keyed on the field being ABSENT, so a second
run is a no-op.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

SUMMARY = '"summary": "Recorded outcome for this fixture section.",'
SUMMARY_STATUS_AWARE = (
    '"summary": "Recorded outcome for this fixture section." '
    'if status in {"completed", "handed_off"} else None,'
)
BLOCKER = '"blocker": "The staging migration needs an owner role this account does not have.",'
BLOCKER_STATUS_AWARE = (
    '"blocker": "The staging migration needs an owner role this account does not have." '
    'if status == "blocked" else None,'
)
TITLE = '"section_title": "Fixture section title",'
EXIT_CODE = '"exit_code": 0,'

TERMINAL = {"completed", "handed_off"}

# Titles a fixture wrote for brevity ("t", "x", "") cannot render. Rewriting them
# is better than failing the fixture: the test is about some other behaviour and
# the title is incidental, so it gets a value that satisfies the contract.
WEAK_TITLES = {"", "t", "x", "T", "X", "tt", "xx", "  "}

# A field can be present and still useless. `summary=""` is the common shape in
# fixture builders that take a summary parameter and default it to empty, and the
# presence-only check walked straight past it.
EMPTY_VALUES = {"", "None", "0"}


def _inline_end(node: ast.Dict) -> int | None:
    """Column just before the closing brace when the literal is on one line."""
    if node.end_lineno != node.lineno:
        return None
    return node.end_col_offset


def keys_of(node: ast.Dict) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value.value if isinstance(value, ast.Constant) else "<expr>"
    return out


def function_status_params(tree: ast.AST) -> list[tuple[int, int, bool]]:
    """(start, end, has_status_param) for every function, 1-based inclusive."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            out.append((node.lineno, node.end_lineno or node.lineno, "status" in names))
    return out


def plan_for(path: pathlib.Path, empty_fills: dict[str, str] | None = None) -> tuple[list, int]:
    """Return [(lineno, col_offset, text)] insertions for one file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Inside a function that takes `status`, a literal status key means the field
    # should be status-aware rather than unconditional.
    status_spans = [(s, e) for s, e, has in function_status_params(tree) if has]

    def in_status_function(lineno: int) -> bool:
        return any(start <= lineno <= end for start, end in status_spans)

    # (lineno, end_col_offset_or_None, text). A None end column means the literal
    # spans lines, so the field goes on its own line; an int means the dict closes
    # on that line at that column and the field goes inline BEFORE it. Using the
    # AST's own offsets rather than sniffing for a trailing "}" matters because
    # these dicts are frequently nested inside a call on the same line:
    #     _call_tool(server, 3, "agentacct_record_section", {"source": ...})
    insertions: list[tuple[int, int, int, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = keys_of(node)
        text: str | None = None

        # `f"section_{status}"` on the *event type* with `section_status: status`
        # in metadata is the other common builder shape; treat any non-literal
        # status inside a status-taking function as status-aware.
        literal_status = keys.get("section_status")
        if "section_status" in keys:
            aware = literal_status == "<expr>" and in_status_function(node.lineno)
            weak = str(keys.get("section_title")) in WEAK_TITLES or str(keys.get("title")) in WEAK_TITLES
            if ("section_title" not in keys and "title" not in keys) or weak:
                insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, TITLE))
            if literal_status in TERMINAL and str(keys.get("summary")) in EMPTY_VALUES:
                insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, SUMMARY_STATUS_AWARE if aware else SUMMARY))
            elif aware and str(keys.get("summary")) in EMPTY_VALUES:
                insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, SUMMARY_STATUS_AWARE))
            if literal_status == "blocked" and str(keys.get("blocker")) in EMPTY_VALUES:
                insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, BLOCKER))
            elif aware and str(keys.get("blocker")) in EMPTY_VALUES:
                insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, BLOCKER_STATUS_AWARE))

        if keys.get("sentinel_semantic_kind") == "evidence" and not any(
            key in keys
            for key in ("command", "files", "artifact_ref", "artifact_path", "artifact_url", "exit_code")
        ):
            insertions.append((node.lineno, node.end_lineno or node.lineno, node.col_offset, node.end_col_offset, EXIT_CODE))

    # The same line can collect several insertions; keep a stable order so the
    # diff is readable and re-running is idempotent.
    # Merge insertions that target the SAME dict: overlapping segment
    # replacements would otherwise fight over the same text.
    merged: dict[tuple[int, int, int, int], list[str]] = {}
    for start_line, end_line, start_col, end_col, text in insertions:
        merged.setdefault((start_line, end_line, start_col, end_col), []).append(text)
    source_lines = source.split("\n")
    plan = []
    for (start_line, end_line, start_col, end_col), texts in merged.items():
        if start_line == end_line:
            segment = source_lines[start_line - 1][start_col:end_col]
        else:
            block = [source_lines[start_line - 1][start_col:]]
            block.extend(source_lines[start_line:end_line - 1])
            block.append(source_lines[end_line - 1][:end_col])
            segment = "\n".join(block)
        plan.append((start_line, end_line, start_col, end_col, texts, segment))
    return plan, source.count("\n") + 1


def apply_empty_fills(source: str, empty_fills: dict[str, str]) -> str:
    """Give a present-but-empty field a usable value.

    Handles the fixture-builder shape `summary=""` (and its status-aware cousin
    `summary=summary`), which key-presence checks cannot see.
    """
    for key, value in empty_fills.items():
        source = re.sub(rf'("{key}":\s*)""', rf"\g<1>{value}", source)
    return source


def apply(path: pathlib.Path, plan, dry_run: bool, empty_fills: dict[str, str] | None = None) -> int:
    """Insert the planned fields by replacing each literal's exact source text.

    Two earlier versions inserted at recorded line/column offsets and both were
    wrong for the same underlying reason: editing one literal moves the offsets
    recorded for every literal that contains it or follows it. Replacing the
    literal's own TEXT is immune to that -- the search re-locates it after each
    edit -- and an occurrence counter disambiguates a literal whose text repeats.

    Nested literals are handled inner-first, because extending an outer literal
    rewrites the inner one's text.
    """
    source = path.read_text(encoding="utf-8")
    ordered = sorted(plan, key=lambda item: (item[0], -item[1], item[2]))
    applied = 0
    seen: dict[str, int] = {}
    for start_line, end_line, start_col, end_col, additions, segment in ordered:
        index = seen.get(segment, 0)
        position = -1
        for _ in range(index + 1):
            position = source.find(segment, position + 1)
            if position < 0:
                break
        if position < 0:
            continue
        seen[segment] = index + 1
        source = source[:position] + _extend_dict(segment, additions) + source[position + len(segment):]
        applied += len(additions)
    if empty_fills:
        source = apply_empty_fills(source, empty_fills)
    ast.parse(source)  # never write a file that does not parse
    if not dry_run:
        path.write_text(source, encoding="utf-8")
    return applied


def _extend_dict(segment: str, additions: list[str]) -> str:
    """Add fields to a dict literal, on one line or many.

    Single line:  `{"a": 1}` + ['"b": 2']  -> `{"a": 1, "b": 2}`
    Multi line:   the new fields go on their own lines before the closing brace,
                  reusing the literal's closing-brace indentation.
    """
    body = segment.rstrip()
    assert body.endswith("}"), body
    if "\n" not in body:
        inner = body[:-1].rstrip()
        # Normalize the separator in one place. Leaving an existing trailing comma
        # in place and then adding ", " produced `"a": 1,, "b": 2` -- the doubled
        # comma that broke two earlier attempts at this tool.
        if inner.endswith(","):
            inner = inner[:-1].rstrip()
        # Each addition is written as a source line ending in a comma (they are
        # also emitted on their own lines in the multi-line branch), so joining
        # them with ", " produced the `,,` seen in the output. Strip the trailing
        # comma per field and let the join own the separators.
        joined = ", ".join(text.rstrip().rstrip(",") for text in additions)
        if inner.endswith("{"):
            return f"{inner}{joined}}}"
        return f"{inner}, {joined}}}"
    lines = body.split("\n")
    closing = lines[-1]
    indent = closing[: len(closing) - len(closing.lstrip())]
    field_indent = indent + "    "
    last = lines[-2].rstrip() if len(lines) >= 2 else ""
    if last and not last.endswith(",") and not last.endswith("{"):
        lines[-2] = last + ","
    for text in additions:
        lines.insert(-1, f"{field_indent}{text}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", default=["tests"], help="files or directories (default: tests)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip", nargs="*", default=[
        "test_data_quality_rules.py", "test_rules_fuzz.py", "test_text_hygiene.py", "test_display_alignment.py",
    ], help="files already written against the contract")
    args = parser.parse_args()

    targets: list[pathlib.Path] = []
    for raw in args.paths:
        target = pathlib.Path(raw)
        if target.is_dir():
            targets.extend(sorted(target.glob("*.py")))
        elif target.is_file():
            targets.append(target)

    total = 0
    for path in targets:
        if path.name in args.skip:
            continue
        empty_fills: dict[str, str] = {}
        try:
            insertions, _ = plan_for(path, empty_fills)
        except SyntaxError as exc:
            print(f"  SKIP {path.name}: {exc}")
            continue
        if not insertions and not empty_fills:
            continue
        try:
            added = apply(path, insertions, args.dry_run, empty_fills)
        except SyntaxError as exc:
            print(f"  SKIP {path.name} (would not parse): {exc}")
            continue
        total += added
        print(f"  {path.name}: +{added} field(s){' (dry run)' if args.dry_run else ''}")
    print(f"total fields added: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
