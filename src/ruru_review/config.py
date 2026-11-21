"""Settings: defaults, then ``[tool.ruru]`` in the checked project's ``pyproject.toml``, then the command line."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

AGENT_BOTS = [
    "copilot-swe-agent[bot]",
    "claude[bot]",
    "devin-ai-integration[bot]",
    "cursor[bot]",
    "codex[bot]",
    "openhands-agent[bot]",
]
AGENT_TRAILERS = [
    "noreply@anthropic.com",
    "copilot-swe-agent",
    "devin-ai-integration",
    "cursoragent@cursor.com",
    "codex@openai.com",
    "openhands@all-hands.dev",
    "aider",
]
AGENT_BRANCHES = ["claude/", "copilot/", "codex/", "cursor/", "devin/"]
AGENT_LABELS = ["agent"]


@dataclass
class Gate:
    """When an agent pull request fails."""

    tamper_high: bool = True
    fail_first: bool = True
    min_score: float = 0.70
    min_mutants: int = 5


@dataclass
class Agents:
    """How ruru tells an agent's pull request from a person's."""

    bots: list[str] = field(default_factory=lambda: list(AGENT_BOTS))
    trailers: list[str] = field(default_factory=lambda: list(AGENT_TRAILERS))
    branches: list[str] = field(default_factory=lambda: list(AGENT_BRANCHES))
    labels: list[str] = field(default_factory=lambda: list(AGENT_LABELS))


@dataclass
class Config:
    """Every setting a run uses."""

    base: str | None = None
    test_command: str | None = None
    src: str | None = None
    tests: list[str] = field(default_factory=lambda: ["tests", "test"])
    max_mutants: int = 60
    time_budget: float = 600.0
    operators: list[str] | None = None
    arid: list[str] = field(default_factory=list)
    comment_cap: int = 5
    workers: int = 2
    gate: Gate = field(default_factory=Gate)
    agents: Agents = field(default_factory=Agents)


def _merge(obj: Any, table: dict[str, Any], where: str) -> None:
    """
    Copy known keys of a TOML table onto a dataclass, ``-`` read as ``_``.

    :param obj: The dataclass instance
    :param table: The TOML table
    :param where: Table name for error messages
    :raises ValueError: On an unknown key
    """
    names = {f.name for f in fields(obj)}
    for key, value in table.items():
        name = key.replace("-", "_")
        if name not in names:
            raise ValueError(f"unknown key {key!r} in [{where}]")
        current = getattr(obj, name)
        if isinstance(current, (Gate, Agents)):
            _merge(current, value, f"{where}.{key}")
        else:
            setattr(obj, name, value)


def load(root: Path) -> Config:
    """
    Read ``[tool.ruru]`` from ``root/pyproject.toml``.

    :param root: The checked project's root
    :returns: The settings
    """
    cfg = Config()
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        table = data.get("tool", {}).get("ruru", {})
        if table:
            _merge(cfg, table, "tool.ruru")
    return cfg
