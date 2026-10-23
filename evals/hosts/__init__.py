from .base import SKILL_NAME, Host, RunContext, RunResult, ToolCall
from .claude_code import ClaudeCode
from .codex import Codex

HOSTS: dict[str, Host] = {h.name: h for h in (ClaudeCode(), Codex())}

__all__ = ["HOSTS", "SKILL_NAME", "Host", "RunContext", "RunResult", "ToolCall"]
