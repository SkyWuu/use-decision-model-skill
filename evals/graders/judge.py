"""LLM judges. Each report is graded twice, by an Opus judge (Claude Code CLI) and a GPT judge
(Codex CLI), against the case's rubric items. An item passes only when both judges pass it; a split
verdict is recorded as "disputed" and counted as a failure until a person reviews it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "pass", "evidence"],
                "properties": {"id": {"type": "string"}, "pass": {"type": "boolean"}, "evidence": {"type": "string"}},
            },
        }
    },
}

PROMPT_TEMPLATE = """你是严格、公正的评审。下面是一份 AI 助手写的分析报告：它只读分析了一个样例代码仓库，回答“这个项目哪里可以用决策模型（decision model）”。决策模型读入状态和封闭题目，输出是非题概率、选择题或等级题的答案，不生成文本。

请逐条判断评分项是否满足。

## 样例仓库的真实情况（报告作者看不到这一部分，只用来帮你判断）

{context}

## 评分项

{items}

## 判定要求

- 只看报告写了什么，不替作者补充、不猜测意图。
- 每个评分项给出 pass（true 或 false）和 evidence：通过时引用报告里的关键原句（80 字以内）；不通过时写清缺了什么或错在哪里。
- 评分项自带的条件（例如“建议清单为空时判定为通过”）按条件执行。
- 必须对上面列出的每一个评分项都给出结果，id 原样照抄。

## 报告

<report>
{report}
</report>
"""


def build_prompt(context: str, items: list[dict], report: str) -> str:
    lines = [f"- {it['id']}：{it['text'].strip()}" for it in items]
    return PROMPT_TEMPLATE.format(context=context.strip() or "（无）", items="\n".join(lines), report=report.strip())


def _claude_bin() -> str:
    return os.environ.get("UDM_CLAUDE_BIN") or "claude"


def _codex_bin() -> str:
    return os.environ.get("UDM_CODEX_BIN") or "codex"


def run_opus(prompt: str, model: str, timeout: int = 600) -> dict:
    with tempfile.TemporaryDirectory(prefix="udm-judge-opus-") as tmp:
        cfg = Path(tmp) / "cfg"
        cfg.mkdir()
        cmd = [
            _claude_bin(), "-p", prompt, "--model", model, "--output-format", "json", "--tools", "",
            "--no-session-persistence", "--json-schema", json.dumps(SCHEMA),
        ]
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
        try:
            proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "items": []}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"error": f"bad output: {proc.stdout[-300:]} {proc.stderr[-300:]}", "items": []}
        out = data.get("structured_output") or {}
        if not out and data.get("result"):
            try:
                out = json.loads(data["result"])
            except json.JSONDecodeError:
                out = {}
        return {"items": out.get("items", []), "cost_usd": data.get("total_cost_usd"),
                "error": None if out.get("items") else (data.get("subtype") or "no items")}


def run_gpt(prompt: str, model: str, timeout: int = 900) -> dict:
    with tempfile.TemporaryDirectory(prefix="udm-judge-gpt-") as tmp:
        home = Path(tmp) / "codex-home"
        home.mkdir()
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(SCHEMA), "utf-8")
        out_path = Path(tmp) / "out.json"
        work = Path(tmp) / "work"
        work.mkdir()
        cmd = [
            _codex_bin(), "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-C", str(work),
            "-m", model, "-c", 'model_reasoning_effort="medium"', "-s", "read-only",
            "--output-schema", str(schema_path), "-o", str(out_path), prompt,
        ]
        env = dict(os.environ, CODEX_HOME=str(home))
        try:
            proc = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"error": "timeout", "items": []}
        if not out_path.exists():
            return {"error": f"no output: {proc.stderr[-300:]}", "items": []}
        try:
            out = json.loads(out_path.read_text("utf-8"))
        except json.JSONDecodeError:
            return {"error": "bad json", "items": []}
        return {"items": out.get("items", []), "error": None if out.get("items") else "no items"}


def combine(items: list[dict], verdicts: dict[str, dict]) -> list[dict]:
    """verdicts: judge name -> run_* result. Returns one row per rubric item."""
    rows = []
    for it in items:
        row = {"id": it["id"], "category": it["category"], "skill_only": it.get("skill_only", False), "judges": {}}
        votes = []
        for judge, result in verdicts.items():
            found = next((r for r in result.get("items", []) if r.get("id") == it["id"]), None)
            vote = bool(found and found.get("pass"))
            votes.append(vote)
            row["judges"][judge] = {"pass": vote, "evidence": (found or {}).get("evidence", "missing")}
        if votes and all(votes):
            row["final"] = "pass"
        elif not any(votes):
            row["final"] = "fail"
        else:
            row["final"] = "disputed"
        rows.append(row)
    return rows
