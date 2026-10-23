"""Claude Code adapter (`claude -p`).

Isolation: CLAUDE_CONFIG_DIR points at a private directory, so user-level skills, CLAUDE.md and
settings are not loaded; the skill arm installs the skill at <config>/skills/use-decision-model.

Permissions: file edits inside the workspace are auto-accepted (acceptEdits) so that a skill which
writes files shows up in the workspace diff; shell commands outside a read-only allowlist are
denied automatically and recorded in `permission_denials`.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import SKILL_NAME, Host, RunContext, RunResult, ToolCall, base_env, copy_skill, read_jsonl

READ_ONLY_BASH = [
    "rg", "grep", "ls", "find", "cat", "head", "tail", "wc", "awk", "sort", "uniq", "cut", "tr", "nl",
    "sed -n", "cd", "pwd", "echo", "file", "tree", "du", "stat",
    "git log", "git show", "git rev-parse", "git rev-list", "git ls-files", "git blame", "git grep",
    "git status", "git diff", "git branch", "git describe", "git shortlog", "git config --get",
    # agents often address the repository explicitly; write subcommands still show up in the diff and D3
    "git -C", "git --no-pager",
]
ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Task", "Skill", "TodoWrite"] + [f"Bash({c}:*)" for c in READ_ONLY_BASH]


class ClaudeCode(Host):
    name = "claude_code"
    invocation = f"/{SKILL_NAME} "
    default_model = os.environ.get("UDM_CLAUDE_MODEL", "claude-opus-5-5")

    def binary(self) -> str | None:
        return os.environ.get("UDM_CLAUDE_BIN") or "claude"

    def config_dir(self, ctx: RunContext) -> Path:
        d = ctx.home / "claude-config"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def install_skill(self, ctx: RunContext, skill_dir: Path) -> Path:
        return copy_skill(skill_dir, self.config_dir(ctx) / "skills")

    def command(self, ctx: RunContext, prompt: str) -> tuple[list[str], dict[str, str]]:
        (self.config_dir(ctx) / "skills").mkdir(exist_ok=True)
        cmd = [
            self.binary(), "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--model", ctx.model,
            "--permission-mode", "acceptEdits",
            "--permission-prompts", "none",
            "--allowedTools", " ".join(ALLOWED_TOOLS),
            "--max-budget-usd", f"{ctx.budget_usd:g}",
            "--no-session-persistence",
            # the installed skill lives outside the workspace; let read-only commands reach it
            "--add-dir", str(self.config_dir(ctx) / "skills"),
        ]
        return cmd, base_env(CLAUDE_CONFIG_DIR=str(self.config_dir(ctx)))

    def parse(self, ctx: RunContext, stdout_path: Path) -> RunResult:
        events = read_jsonl(stdout_path)
        res = RunResult()
        calls: dict[str, ToolCall] = {}
        order: list[str] = []
        last_text = ""
        for e in events:
            etype = e.get("type")
            if etype == "assistant":
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        last_text = c["text"]
                    if c.get("type") != "tool_use":
                        continue
                    inp = c.get("input") or {}
                    call = ToolCall(
                        tool=c.get("name", ""),
                        command=inp.get("command"),
                        path=inp.get("file_path") or inp.get("path") or inp.get("notebook_path"),
                        raw=inp,
                    )
                    tid = c.get("id") or f"call-{len(order)}"
                    calls[tid] = call
                    order.append(tid)
                    if call.tool == "Skill" and SKILL_NAME in str(inp):
                        res.skill_invoked = True
                    if f"skills/{SKILL_NAME}/" in (call.path or "") + (call.command or ""):
                        res.skill_invoked = True
            elif etype == "user":
                content = e.get("message", {}).get("content", [])
                if isinstance(content, str):
                    if f"skills/{SKILL_NAME}" in content or "Base directory for this skill" in content:
                        res.skill_invoked = True
                    continue
                for c in content:
                    if c.get("type") == "tool_result" and c.get("is_error") and c.get("tool_use_id") in calls:
                        calls[c["tool_use_id"]].failed = True
                    if c.get("type") == "text" and f"skills/{SKILL_NAME}" in c.get("text", ""):
                        res.skill_invoked = True
            elif etype == "result":
                res.report = e.get("result") or last_text
                res.cost_usd = e.get("total_cost_usd")
                res.turns = e.get("num_turns")
                usage = e.get("usage") or {}
                res.input_tokens = sum(
                    usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                )
                res.output_tokens = usage.get("output_tokens")
                if e.get("is_error"):
                    res.error = e.get("subtype") or "error"
                for d in e.get("permission_denials") or []:
                    tid = d.get("tool_use_id") or f"denied-{len(order)}"
                    if tid in calls:
                        calls[tid].denied = True
                    else:
                        inp = d.get("tool_input") or {}
                        calls[tid] = ToolCall(
                            tool=d.get("tool_name", ""), command=inp.get("command"),
                            path=inp.get("file_path"), denied=True, raw=inp,
                        )
                        order.append(tid)
        if not res.report:
            res.report = last_text
            res.error = res.error or "no result event"
        res.tool_calls = [calls[i] for i in order if i in calls]
        return res
