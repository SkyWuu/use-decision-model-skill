"""Codex adapter (`codex exec`).

Isolation: CODEX_HOME points at a private directory, so the user's config.toml, AGENTS.md and
$CODEX_HOME/skills are not loaded; the skill arm installs the skill at
$CODEX_HOME/skills/use-decision-model. $HOME stays as is, so skills in ~/.agents/skills are visible
to both arms (see RESULTS.md).

A fresh CODEX_HOME has no login: set CODEX_API_KEY, or point UDM_CODEX_BIN at a command that brings
its own login.

Permissions: workspace-write sandbox, so a skill that writes files shows up in the workspace diff;
network is off inside the sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import SKILL_NAME, Host, RunContext, RunResult, ToolCall, base_env, copy_skill, read_jsonl


class Codex(Host):
    name = "codex"
    invocation = f"${SKILL_NAME} "
    default_model = os.environ.get("UDM_CODEX_MODEL", "gpt-6-astra")
    reasoning_effort = os.environ.get("UDM_CODEX_EFFORT", "high")

    def binary(self) -> str | None:
        return os.environ.get("UDM_CODEX_BIN") or "codex"

    def codex_home(self, ctx: RunContext) -> Path:
        d = ctx.home / "codex-home"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def install_skill(self, ctx: RunContext, skill_dir: Path) -> Path:
        return copy_skill(skill_dir, self.codex_home(ctx) / "skills")

    def command(self, ctx: RunContext, prompt: str) -> tuple[list[str], dict[str, str]]:
        cmd = [
            self.binary(), "exec",
            "--json", "--ephemeral", "--skip-git-repo-check",
            "-C", str(ctx.ws),
            "-m", ctx.model,
            "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
            "-s", "workspace-write",
            "-o", str(ctx.out / "last_message.txt"),
            prompt,
        ]
        return cmd, base_env(CODEX_HOME=str(self.codex_home(ctx)))

    def parse(self, ctx: RunContext, stdout_path: Path) -> RunResult:
        res = RunResult()
        last_message = ""
        in_tok = out_tok = 0
        for e in read_jsonl(stdout_path):
            etype = e.get("type")
            if etype == "item.completed":
                item = e.get("item") or {}
                itype = item.get("type")
                if itype == "agent_message":
                    last_message = item.get("text") or last_message
                elif itype == "command_execution":
                    cmd = item.get("command") or ""
                    res.tool_calls.append(ToolCall(
                        tool="shell", command=cmd,
                        failed=item.get("status") == "failed" or (item.get("exit_code") not in (0, None)),
                        raw={k: item.get(k) for k in ("command", "exit_code", "status")},
                    ))
                    if f"{SKILL_NAME}/SKILL.md" in cmd:
                        res.skill_invoked = True
                elif itype == "file_change":
                    for ch in item.get("changes") or [{}]:
                        res.tool_calls.append(ToolCall(
                            tool="file_change", path=ch.get("path"),
                            raw={"kind": ch.get("kind"), "status": item.get("status")},
                        ))
                elif itype in ("mcp_tool_call", "web_search"):
                    res.tool_calls.append(ToolCall(tool=itype, raw={k: v for k, v in item.items() if k != "id"}))
            elif etype == "turn.completed":
                usage = e.get("usage") or {}
                in_tok += usage.get("input_tokens") or 0
                out_tok += (usage.get("output_tokens") or 0)
                res.error = None  # transient stream errors before a completed turn do not count
            elif etype in ("turn.failed", "error"):
                res.error = str(e.get("error") or e.get("message") or etype)[:500]
        last_file = ctx.out / "last_message.txt"
        res.report = last_file.read_text("utf-8") if last_file.exists() else last_message
        res.input_tokens, res.output_tokens = in_tok, out_tok
        if not res.report.strip() and not res.error:
            res.error = "empty report"
        return res
