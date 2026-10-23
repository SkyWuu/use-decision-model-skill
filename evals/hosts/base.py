"""Common pieces for host adapters.

A host adapter knows three things about one agent CLI: where a skill must be installed so the CLI
loads it without touching the user's real configuration, how to start a headless run, and how to
turn the CLI's event stream into a report, a list of tool calls and a cost.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

SKILL_NAME = "use-decision-model"


@dataclass
class RunContext:
    """Everything an adapter needs for one run. `ws` is a throwaway copy of the fixture;
    `home` is a private directory for the host's configuration."""

    ws: Path
    home: Path
    out: Path
    model: str
    budget_usd: float
    with_skill: bool


@dataclass
class ToolCall:
    tool: str
    command: str | None = None
    path: str | None = None
    denied: bool = False
    failed: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "command": self.command,
            "path": self.path,
            "denied": self.denied,
            "failed": self.failed,
        }


@dataclass
class RunResult:
    report: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    error: str | None = None
    skill_invoked: bool = False


class Host:
    name = "base"
    #: prefix that explicitly invokes the skill in this host's prompt syntax
    invocation = f"/{SKILL_NAME} "
    default_model = ""

    def binary(self) -> str | None:
        raise NotImplementedError

    def available(self) -> bool:
        b = self.binary()
        return bool(b and shutil.which(b))

    def install_skill(self, ctx: RunContext, skill_dir: Path) -> Path:
        raise NotImplementedError

    def command(self, ctx: RunContext, prompt: str) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def parse(self, ctx: RunContext, stdout_path: Path) -> RunResult:
        raise NotImplementedError


def copy_skill(skill_dir: Path, dest_parent: Path) -> Path:
    dest = dest_parent / SKILL_NAME
    if dest.exists():
        shutil.rmtree(dest)
    dest_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_dir, dest)
    return dest


def read_jsonl(path: Path) -> list[dict]:
    events = []
    if not path.exists():
        return events
    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def base_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(overrides)
    return env
