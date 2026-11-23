"""The launch context this compatibility package takes its locations from.

Nonoka code; not a copy of anything in FineSub. The package's bridge binds
it from the launch context (`paths.package_data`) before anything imports
`finesub`, so the engine never falls back to a location of its own: a
process that was not given one fails here, saying so, instead of writing to
``%LOCALAPPDATA%\\FineSub``.

An agent CLI (Antigravity, Claude, Codex, etc.) spawns FineSub's MCP server
(`python -m finesub.llm.agent.agent_mcp_server`) directly rather than through
Nonoka's engine host, often passing only the `FINESUB_MCP_*` environment FineSub
wrote for the server. Every live assignment root sits under
``<package_data>/agent-capsules/...``, so when `_PACKAGE_DATA` was not bound
and `FINESUB_MCP_SESSION` + `FINESUB_MCP_ROOT` are present, `package_data()`
recovers `<package_data>` from `FINESUB_MCP_ROOT` instead of crashing when a
validator imports `finesub.llm.stages` (and thus `knowledge.base`).
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_DATA: Path | None = None
_MCP_SESSION_ENV = "FINESUB_MCP_SESSION"
_MCP_ROOT_ENV = "FINESUB_MCP_ROOT"
_AGENT_CAPSULES_DIR = "agent-capsules"


def bind(package_data: str | Path) -> None:
    """Bind the package's data directory, ``engines\\<package>\\`` in Nonoka's data directory."""

    global _PACKAGE_DATA
    _PACKAGE_DATA = Path(package_data).expanduser().resolve()


def _mcp_child_package_data() -> Path | None:
    """Recover `<package_data>` for an MCP server child spawned by an agent CLI."""

    if not os.environ.get(_MCP_SESSION_ENV):
        return None
    mcp_root = os.environ.get(_MCP_ROOT_ENV)
    if not mcp_root:
        return None
    resolved = Path(mcp_root).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        name = candidate.name.lower() if os.name == "nt" else candidate.name
        if name == _AGENT_CAPSULES_DIR and candidate.parent != candidate:
            return candidate.parent
    return None


def package_data() -> Path:
    if _PACKAGE_DATA is not None:
        return _PACKAGE_DATA
    recovered = _mcp_child_package_data()
    if recovered is not None:
        return recovered
    raise RuntimeError(
        "finesub_bootstrap (compatibility package): no package data directory was bound from the "
        "launch context (paths.package_data); this process was not started by Nonoka's engine host"
    )
