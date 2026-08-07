"""PyInstaller entry point for the standalone agentacct CLI.

A frozen build has no console_scripts shim and is NOT a Python interpreter, yet
the recording hooks agentacct installs invoke their command as if it were one:
Claude Code's ``settings.json`` runs ``<cli> -m agentacct.statusline_hook`` and
``<cli> <hooks>/claude_pre_tool_use.py`` (onboard fills ``<cli>`` from
``sys.executable`` — a real ``python`` for a pip/pipx install, but the frozen
binary itself here). So this entry emulates the two interpreter forms the hooks
rely on, then falls through to the normal Typer CLI for everything else:

  agentacct -m some.module [args]   -> runpy.run_module(..., "__main__")
  agentacct /path/to/script.py …    -> runpy.run_path(..., "__main__")
  agentacct <command> …             -> the agentacct.cli Typer app

This keeps ONE binary working as both the CLI (MCP server, daemon, onboard) and
the hook runner, with no change to onboard's command generation or to existing
pip/pipx installs (which point the same hook commands at a real python).
"""
from __future__ import annotations

import multiprocessing
import os
import runpy
import sys


def _run_as_interpreter() -> bool:
    """Handle the ``python``-interpreter forms the hooks use. Returns True if it
    dispatched (the process should exit), False to fall through to the CLI."""

    argv = sys.argv
    if len(argv) >= 3 and argv[1] == "-m":
        # `<cli> -m module [args]` -> run that module as __main__.
        module = argv[2]
        sys.argv = [module, *argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return True
    if len(argv) >= 2:
        first = argv[1]
        # `<cli> script.py [args]` -> run that file as __main__. Guard on a real
        # .py file so a Typer subcommand that merely ends in ".py" is impossible
        # to confuse (Typer command names never do).
        if first.endswith(".py") and os.path.isfile(first):
            sys.argv = [first, *argv[2:]]
            runpy.run_path(first, run_name="__main__")
            return True
    return False


def main() -> None:
    # uvicorn/textual may spawn; freeze_support keeps a frozen re-exec sane.
    multiprocessing.freeze_support()
    if _run_as_interpreter():
        return
    from agentacct.cli import app

    app()


if __name__ == "__main__":
    main()
