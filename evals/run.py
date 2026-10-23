#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Eval runner for the use-decision-model skill.

    uv run evals/run.py build                         # build fixtures, fetch pinned real repositories, into evals/.work
    uv run evals/run.py run --tag run1 --hosts claude_code,codex --repeats 2 --jobs 4
    uv run evals/run.py grade --tag run1              # deterministic checks + Opus and GPT judges
    uv run evals/run.py report --tag run1             # tables in evals/runs/run1/summary.md
    uv run evals/run.py trigger --tag trigger1 --hosts claude_code,codex

Every run gets a fresh copy of the fixture and a private configuration directory for the host, so
earlier runs and the user's own host configuration do not leak in. Results go to evals/runs/<tag>/;
runs that already finished there are skipped, so use a new tag for each round.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

EVALS = Path(__file__).resolve().parent
REPO = EVALS.parent
SKILL_DIR = Path(os.environ.get("UDM_SKILL_DIR") or REPO / "skills" / "use-decision-model")
WORK = EVALS / ".work"
RUNS = EVALS / "runs"
TMP_ROOT = Path(os.environ.get("UDM_TMP", tempfile.gettempdir())) / "udm-eval"

sys.path.insert(0, str(EVALS))
from graders import deterministic, judge  # noqa: E402
from hosts import HOSTS, RunContext  # noqa: E402

_lock = threading.Lock()
_spent = {"usd": 0.0}


def load_cfg() -> dict:
    return yaml.safe_load((EVALS / "cases.yaml").read_text("utf-8"))


def case_prompt(cfg: dict, case: dict) -> str:
    reader = case.get("reader", cfg.get("reader", ""))
    return " ".join(p for p in (cfg["prompt"].strip(), reader.strip()) if p)


def load_rubric(name: str) -> dict:
    return yaml.safe_load((EVALS / "graders" / "rubrics" / f"{name}.yaml").read_text("utf-8"))


def log(msg: str) -> None:
    with _lock:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def skill_version() -> str:
    h = hashlib.sha256()
    for p in sorted(SKILL_DIR.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(SKILL_DIR)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:10]


def snapshot_skill(tag: str) -> None:
    dest = RUNS / tag / "skill-snapshot"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SKILL_DIR, dest)


# ------------------------------------------------------------------ fixtures


def build_fixtures(names: list[str], rebuild: bool = False) -> None:
    pinned = {c["fixture"]: c for c in load_cfg()["cases"] if c.get("repo")}
    for name in names:
        dest = WORK / "fixtures" / name
        if dest.exists() and not rebuild:
            continue
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if name in pinned:
            pin_repo(dest, pinned[name])
        else:
            subprocess.run(["bash", str(EVALS / "fixtures" / name / "build.sh"), str(dest)], check=True)
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
        log(f"built fixture {name} at {head.stdout.strip()}")


def pin_repo(dest: Path, case: dict) -> None:
    """A real repository at a fixed commit: the commit and its whole history on the branch name the
    project uses, with no remote, no tags and nothing newer, so `git log` shows what its authors saw."""
    dest.mkdir()
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(dest), *args], check=True, env=env)

    git("init", "--quiet", "--initial-branch", case.get("branch", "main"))
    git("fetch", "--quiet", "--no-tags", case["repo"], case["commit"])
    git("checkout", "--quiet", "-B", case.get("branch", "main"), case["commit"])
    (dest / ".git" / "FETCH_HEAD").unlink(missing_ok=True)


# ------------------------------------------------------------------ capability profile


def apply_profile(skill_copy: Path, profile: str) -> None:
    """Splice evals/profiles/<profile>.yaml into the installed copy's capability profile."""
    target = skill_copy / "references" / "capability-profile.md"
    text = target.read_text("utf-8")
    variant = (EVALS / "profiles" / f"{profile}.yaml").read_text("utf-8")
    variant_body = "\n".join(line for line in variant.splitlines() if not line.startswith("#")).strip()
    match = re.search(r"```yaml\n(.*?)```", text, re.S)
    if not match:
        raise RuntimeError("capability-profile.md has no yaml block")
    if profile == "unfilled":
        shipped = yaml.safe_load(match.group(1))
        if shipped != yaml.safe_load(variant_body):
            log("WARNING: profiles/unfilled.yaml differs from the shipped capability profile")
        return
    target.write_text(text[: match.start(1)] + variant_body + "\n" + text[match.end(1):], "utf-8")


# ------------------------------------------------------------------ workspace snapshots


def _git(ws: Path, *args: str) -> str:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True, env=env).stdout.strip()


def snapshot(ws: Path) -> dict:
    files = {}
    for root, dirs, names in os.walk(ws):
        if ".git" in dirs:
            dirs.remove(".git")
        for n in names:
            p = Path(root) / n
            rel = str(p.relative_to(ws))
            try:
                data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
            except OSError:
                data = b"<unreadable>"
            files[rel] = hashlib.sha256(data).hexdigest()
    git = {
        "head": _git(ws, "rev-parse", "HEAD"),
        "refs": _git(ws, "for-each-ref", "--format=%(refname) %(objectname)"),
        "stash": _git(ws, "stash", "list"),
    }
    return {"files": files, "git": git}


def diff_snapshots(before: dict, after: dict) -> dict:
    b, a = before["files"], after["files"]
    return {
        "added": sorted(set(a) - set(b)),
        "removed": sorted(set(b) - set(a)),
        "modified": sorted(k for k in set(a) & set(b) if a[k] != b[k]),
        "git": {k: {"before": before["git"][k], "after": after["git"][k]}
                for k in before["git"] if before["git"][k] != after["git"][k]},
    }


# ------------------------------------------------------------------ runs


@dataclass
class Spec:
    tag: str
    host: str
    case: dict
    arm: str
    rep: int
    model: str
    budget_usd: float
    timeout: int
    prompt_override: str | None = None
    out_override: Path | None = None

    @property
    def out(self) -> Path:
        if self.out_override:
            return self.out_override
        return RUNS / self.tag / self.host / self.case["id"] / self.arm / f"r{self.rep}"


def baseline_alias(case: dict, cases: list[dict]) -> dict:
    """Baseline runs never see the capability profile, so profile variants with the same fixture and
    reader share one baseline."""
    if case.get("profile") == "unfilled":
        return case
    for c in cases:
        if c["fixture"] == case["fixture"] and c.get("profile") == "unfilled" and c.get("reader") == case.get("reader"):
            return c
    return case


def execute(spec: Spec, cfg: dict, keep: bool = False, force: bool = False) -> dict:
    out = spec.out
    meta_path = out / "meta.json"
    if meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text("utf-8"))
        if meta.get("status") == "ok":
            return meta
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    host = HOSTS[spec.host]
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"{spec.host}-{spec.case['id']}-{spec.arm}-r{spec.rep}-".replace("@", "_"),
                                dir=TMP_ROOT))
    ws, home = tmp / "ws", tmp / "home"
    home.mkdir()
    shutil.copytree(WORK / "fixtures" / spec.case["fixture"], ws, symlinks=True)
    ctx = RunContext(ws=ws, home=home, out=out, model=spec.model, budget_usd=spec.budget_usd,
                     with_skill=spec.arm == "skill")
    if ctx.with_skill:
        installed = host.install_skill(ctx, SKILL_DIR)
        apply_profile(installed, spec.case.get("profile", "unfilled"))
    before = snapshot(ws)
    base_prompt = spec.prompt_override or case_prompt(cfg, spec.case)
    prompt = (host.invocation if ctx.with_skill and not spec.prompt_override else "") + base_prompt
    (out / "prompt.txt").write_text(prompt, "utf-8")
    cmd, env = host.command(ctx, prompt)
    log(f"start {spec.host}/{spec.case['id']}/{spec.arm}/r{spec.rep}")
    t0 = time.time()
    timed_out = False
    with open(out / "stdout.jsonl", "wb") as so, open(out / "stderr.txt", "wb") as se:
        proc = subprocess.Popen(cmd, cwd=ws, env=env, stdout=so, stderr=se, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            rc = proc.wait(timeout=spec.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                rc = proc.wait()
    duration = time.time() - t0
    after = snapshot(ws)
    result = host.parse(ctx, out / "stdout.jsonl")
    diff = diff_snapshots(before, after)
    (out / "report.md").write_text(result.report or "", "utf-8")
    (out / "tool_calls.json").write_text(
        json.dumps([c.to_dict() for c in result.tool_calls], ensure_ascii=False, indent=1), "utf-8")
    (out / "diff.json").write_text(json.dumps(diff, ensure_ascii=False, indent=1), "utf-8")
    status = "ok" if (rc == 0 and not timed_out and result.report.strip() and not result.error) else "error"
    meta = {
        "tag": spec.tag, "host": spec.host, "case": spec.case["id"], "fixture": spec.case["fixture"],
        "profile": spec.case.get("profile"), "arm": spec.arm, "rep": spec.rep, "model": spec.model,
        "skill_version": skill_version() if ctx.with_skill else None,
        "status": status, "exit_code": rc, "timed_out": timed_out, "error": result.error,
        "duration_s": round(duration, 1), "cost_usd": result.cost_usd, "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "turns": result.turns, "skill_invoked": result.skill_invoked,
        "tool_calls": len(result.tool_calls), "workspace": str(ws),
        "workspace_changed": bool(diff["added"] or diff["modified"] or diff["removed"] or diff["git"]),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    with _lock:
        _spent["usd"] += result.cost_usd or 0.0
    log(f"done  {spec.host}/{spec.case['id']}/{spec.arm}/r{spec.rep}: {status} {duration:.0f}s "
        f"${(result.cost_usd or 0):.2f} changed={meta['workspace_changed']}")
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return meta


def cmd_run(args: argparse.Namespace) -> None:
    cfg = load_cfg()
    cases = [c for c in cfg["cases"] if args.cases == "all" or c["id"] in args.cases.split(",")]
    build_fixtures(sorted({c["fixture"] for c in cases}))
    snapshot_skill(args.tag)
    if args.reuse_baseline_from:
        # baseline runs do not depend on the skill, so a skill-only round can borrow them (grades are redone),
        # but only while the user prompt is unchanged
        prompts = {c["id"]: case_prompt(cfg, c) for c in cfg["cases"]}
        stale = set()
        for src in (RUNS / args.reuse_baseline_from).glob("*/*/baseline/r*"):
            dest = RUNS / args.tag / src.relative_to(RUNS / args.reuse_baseline_from)
            used = (src / "prompt.txt").read_text("utf-8") if (src / "prompt.txt").exists() else None
            if used != prompts.get(src.parts[-3]):
                stale.add(src.parts[-3])
            elif not dest.exists():
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns("grades.json"))
        log(f"reused baseline runs from {args.reuse_baseline_from}"
            + (f"; not reused because the prompt changed: {', '.join(sorted(stale))}" if stale else ""))
    specs: list[Spec] = []
    for host_name in args.hosts.split(","):
        host = HOSTS[host_name]
        if not host.available():
            log(f"skip host {host_name}: binary {host.binary()!r} not found")
            continue
        model = getattr(args, f"model_{host_name}", None) or host.default_model
        for case in cases:
            for arm in args.arms.split(","):
                if arm == "baseline" and baseline_alias(case, cfg["cases"]) is not case:
                    continue
                for rep in range(1, args.repeats + 1):
                    specs.append(Spec(args.tag, host_name, case, arm, rep, model, args.budget_usd, args.timeout))
    log(f"{len(specs)} runs scheduled, {args.jobs} at a time")
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = []
        for spec in specs:
            futures.append(pool.submit(_guarded_execute, spec, cfg, args))
        for f in cf.as_completed(futures):
            f.result()
    log(f"finished; Claude-side spend this session ${_spent['usd']:.2f}")


def _guarded_execute(spec: Spec, cfg: dict, args: argparse.Namespace) -> dict | None:
    with _lock:
        over = _spent["usd"] >= args.total_budget_usd
    if over:
        log(f"skip {spec.host}/{spec.case['id']}/{spec.arm}/r{spec.rep}: total budget reached")
        return None
    try:
        return execute(spec, cfg, keep=args.keep, force=args.force)
    except Exception as exc:  # keep the batch going; the run is marked as an error
        log(f"FAILED {spec.host}/{spec.case['id']}/{spec.arm}/r{spec.rep}: {exc!r}")
        return None


# ------------------------------------------------------------------ grading


def iter_run_dirs(tag: str):
    for meta_path in sorted((RUNS / tag).glob("*/*/*/r*/meta.json")):
        yield meta_path.parent, json.loads(meta_path.read_text("utf-8"))


def rubric_for(case: dict, arm: str) -> tuple[str, list[dict]]:
    contexts, items = [], []
    for name in case["rubrics"]:
        r = load_rubric(name)
        if r.get("context"):
            contexts.append(r["context"])
        for it in r.get("items", []):
            if it.get("skill_only") and arm != "skill":
                continue
            items.append(it)
    return "\n".join(contexts), items


def grade_one(run_dir: Path, meta: dict, cfg: dict, judges: list[str], force: bool) -> None:
    grades_path = run_dir / "grades.json"
    if grades_path.exists() and not force:
        return
    case = {c["id"]: c for c in cfg["cases"]}[meta["case"]]
    report = (run_dir / "report.md").read_text("utf-8")
    tool_calls = json.loads((run_dir / "tool_calls.json").read_text("utf-8"))
    diff = json.loads((run_dir / "diff.json").read_text("utf-8"))
    source = str(WORK / "fixtures" / case["fixture"])
    det = deterministic.check(case, cfg, meta["arm"], report, tool_calls, diff, meta.get("workspace", ""), source)
    context, items = rubric_for(case, meta["arm"])
    verdicts = {}
    if report.strip() and judges:
        prompt = judge.build_prompt(context, items, report)
        runners = {
            "opus": lambda: judge.run_opus(prompt, os.environ.get("UDM_JUDGE_OPUS", "claude-opus-5-5")),
            "gpt": lambda: judge.run_gpt(prompt, os.environ.get("UDM_JUDGE_GPT", "gpt-6-astra")),
        }
        wanted = {it["id"] for it in items}

        def ask(j: str) -> dict:
            result = runners[j]()
            # one retry when a judge errors out or skips rubric items
            if result.get("error") or not wanted <= {r.get("id") for r in result.get("items", [])}:
                result = runners[j]()
            return result

        with cf.ThreadPoolExecutor(max_workers=len(judges)) as pool:
            futs = {j: pool.submit(ask, j) for j in judges if j in runners}
            verdicts = {j: f.result() for j, f in futs.items()}
    # without a report every item fails; with `--judges ""` the judge items are left out of the scores
    rows = judge.combine(items, verdicts) if verdicts else [
        {"id": it["id"], "category": it["category"], "skill_only": it.get("skill_only", False),
         "judges": {}, "final": "ungraded" if report.strip() else "fail"} for it in items]
    grades = {
        "deterministic": det,
        "judge": rows,
        "judge_errors": {j: v.get("error") for j, v in verdicts.items() if v.get("error")},
        "judge_cost_usd": sum((v.get("cost_usd") or 0) for v in verdicts.values()),
    }
    grades_path.write_text(json.dumps(grades, ensure_ascii=False, indent=1), "utf-8")
    judged = "judge skipped" if rows and all(r["final"] == "ungraded" for r in rows) else \
        f"judge {sum(r['final'] == 'pass' for r in rows)}/{len(rows)}"
    log(f"graded {meta['host']}/{meta['case']}/{meta['arm']}/r{meta['rep']}: "
        f"det {sum(d['pass'] for d in det)}/{len(det)}, {judged}")


def cmd_grade(args: argparse.Namespace) -> None:
    cfg = load_cfg()
    judges = [j for j in args.judges.split(",") if j]
    runs = [(d, m) for d, m in iter_run_dirs(args.tag) if m.get("status") == "ok" or args.include_errors]
    if args.cases != "all":
        runs = [(d, m) for d, m in runs if m["case"] in args.cases.split(",")]
    # D10 / D11 look up cited paths and commits in the fixture each run started from
    graded = {m["case"] for _, m in runs}
    build_fixtures(sorted({c["fixture"] for c in cfg["cases"] if c["id"] in graded}))
    log(f"grading {len(runs)} runs with judges {judges}")
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for f in cf.as_completed([pool.submit(grade_one, d, m, cfg, judges, args.force) for d, m in runs]):
            f.result()


# ------------------------------------------------------------------ report


def run_scores(grades: dict) -> dict:
    """Strict per-run scores: a disputed judge item counts as a failure."""
    buckets: dict[str, list[bool]] = {"discipline": [], "substance": [], "profile": [], "format": []}
    lenient: list[bool] = []
    for d in grades["deterministic"]:
        buckets.setdefault(d["category"], []).append(d["pass"])
        if d["category"] in ("discipline", "substance"):
            lenient.append(d["pass"])
    for r in grades["judge"]:
        if r["final"] == "ungraded":
            continue
        buckets.setdefault(r["category"], []).append(r["final"] == "pass")
        if r["category"] in ("discipline", "substance"):
            lenient.append(r["final"] != "fail")
    every = [v for vs in buckets.values() for v in vs]
    shared = buckets["discipline"] + buckets["substance"]
    return {
        **{k: (sum(v) / len(v) if v else None) for k, v in buckets.items()},
        "all": sum(every) / len(every) if every else None,
        "shared": sum(shared) / len(shared) if shared else None,
        "lenient": sum(lenient) / len(lenient) if lenient else None,
        "disputed": sum(r["final"] == "disputed" for r in grades["judge"]),
    }


def _mean(xs: list[float | None]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(x: float | None) -> str:
    return "–" if x is None else f"{x:.2f}"


def cmd_report(args: argparse.Namespace) -> None:
    cfg = load_cfg()
    rows: dict[tuple, list[dict]] = {}
    failures: list[str] = []
    item_fail: dict[tuple, int] = {}
    item_total: dict[tuple, int] = {}
    for run_dir, meta in iter_run_dirs(args.tag):
        key = (meta["host"], meta["case"], meta["arm"])
        grades_path = run_dir / "grades.json"
        if meta.get("status") != "ok" or not grades_path.exists():
            failures.append(f"{'/'.join(map(str, key))}/r{meta['rep']}: status={meta.get('status')} "
                            f"error={meta.get('error')} timed_out={meta.get('timed_out')}")
            continue
        grades = json.loads(grades_path.read_text("utf-8"))
        s = run_scores(grades)
        s.update(cost=meta.get("cost_usd"), duration=meta.get("duration_s"),
                 changed=meta.get("workspace_changed"),
                 banned=not next(d for d in grades["deterministic"] if d["id"] == "D5-no-model-or-vendor-name")["pass"])
        rows.setdefault(key, []).append(s)
        for item in grades["deterministic"] + grades["judge"]:
            if item.get("final") == "ungraded":
                continue
            ok = item.get("pass") if "pass" in item else item["final"] == "pass"
            ik = (meta["host"], meta["arm"], item["id"])
            item_total[ik] = item_total.get(ik, 0) + 1
            if not ok:
                item_fail[ik] = item_fail.get(ik, 0) + 1

    hosts = sorted({k[0] for k in rows})
    out = [f"# Eval summary: {args.tag}", ""]
    out.append("Scores are pass rates averaged over repeats. `shared` = discipline + substance items "
               "(graded for both arms); `all` adds the skill-only profile and format items. "
               "Strict scores pass a judge item only when both judges pass it; `lenient` = shared items "
               "where at least one judge passes it. Judge items of runs graded with `--judges \"\"` are "
               "left out.")
    out.append("")
    for host in hosts:
        out.append(f"## {host}")
        out.append("")
        out.append("| case | arm | n | all | shared | lenient | discipline | substance | profile | format | disputed | writes | names | $/run | s/run |")
        out.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for case in cfg["cases"]:
            cid = case["id"]
            for arm in ("skill", "baseline"):
                key_case = baseline_alias(case, cfg["cases"])["id"] if arm == "baseline" else cid
                rs = rows.get((host, key_case, arm))
                if not rs:
                    continue
                label = cid if arm == "skill" or key_case == cid else f"{cid} (= {key_case})"
                out.append(
                    f"| {label} | {arm} | {len(rs)} | {_fmt(_mean([r['all'] for r in rs]))} | "
                    f"{_fmt(_mean([r['shared'] for r in rs]))} | {_fmt(_mean([r['lenient'] for r in rs]))} | "
                    f"{_fmt(_mean([r['discipline'] for r in rs]))} | "
                    f"{_fmt(_mean([r['substance'] for r in rs]))} | {_fmt(_mean([r['profile'] for r in rs]))} | "
                    f"{_fmt(_mean([r['format'] for r in rs]))} | "
                    f"{sum(r['disputed'] for r in rs)} | {sum(bool(r['changed']) for r in rs)} | "
                    f"{sum(r['banned'] for r in rs)} | {_fmt(_mean([r['cost'] for r in rs]))} | "
                    f"{_fmt(_mean([r['duration'] for r in rs]))} |")
        out.append("")
        skill = [r for (h, c, a), rs in rows.items() if h == host and a == "skill" for r in rs]
        base = [r for (h, c, a), rs in rows.items() if h == host and a == "baseline" for r in rs]
        if skill and base:
            out.append(f"Overall {host}: skill shared {_fmt(_mean([r['shared'] for r in skill]))} vs baseline "
                       f"{_fmt(_mean([r['shared'] for r in base]))} (lenient {_fmt(_mean([r['lenient'] for r in skill]))} "
                       f"vs {_fmt(_mean([r['lenient'] for r in base]))}); discipline "
                       f"{_fmt(_mean([r['discipline'] for r in skill]))} vs {_fmt(_mean([r['discipline'] for r in base]))}; "
                       f"substance {_fmt(_mean([r['substance'] for r in skill]))} vs {_fmt(_mean([r['substance'] for r in base]))}.")
            out.append("")
        worst = sorted(((item_fail.get(k, 0) / item_total[k], k) for k in item_total if k[0] == host),
                       reverse=True)
        worst = [(rate, k) for rate, k in worst if rate > 0][:15]
        if worst:
            out.append("Most-failed items:")
            out.append("")
            for rate, (h, arm, iid) in worst:
                out.append(f"- {arm} {iid}: failed {item_fail[(h, arm, iid)]}/{item_total[(h, arm, iid)]}")
            out.append("")
    if failures:
        out.append("## Runs without grades")
        out.append("")
        out += [f"- {f}" for f in failures]
        out.append("")
    text = "\n".join(out)
    (RUNS / args.tag / "summary.md").write_text(text, "utf-8")
    print(text)


# ------------------------------------------------------------------ trigger evals


def cmd_trigger(args: argparse.Namespace) -> None:
    cfg = load_cfg()
    tcfg = yaml.safe_load((EVALS / "trigger_cases.yaml").read_text("utf-8"))
    fixture = tcfg.get("fixture", "support-desk-py")
    build_fixtures([fixture])
    case = {"id": "trigger", "fixture": fixture, "profile": "unfilled"}
    specs = []
    for host_name in args.hosts.split(","):
        host = HOSTS[host_name]
        if not host.available():
            log(f"skip host {host_name}")
            continue
        model = getattr(args, f"model_{host_name}", None) or host.default_model
        for i, tc in enumerate(tcfg["prompts"]):
            for rep in range(1, args.repeats + 1):
                out = RUNS / args.tag / host_name / "trigger" / f"p{i:02d}" / f"r{rep}"
                specs.append((tc, Spec(args.tag, host_name, case, "skill", rep, model, args.budget_usd,
                                       args.timeout, prompt_override=tc["prompt"], out_override=out)))
    log(f"{len(specs)} trigger runs")
    # once the skill has loaded, its read-only rules apply even though nobody invoked it explicitly
    traps = next((c for c in cfg["cases"] if c["fixture"] == fixture and c.get("profile") == "unfilled"), {})
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(_guarded_execute, spec, cfg, args): (tc, spec) for tc, spec in specs}
        for f in cf.as_completed(futs):
            tc, spec = futs[f]
            meta = f.result() or {}
            row = {"host": spec.host, "prompt": tc["prompt"], "expected": tc["should_trigger"],
                   "triggered": bool(meta.get("skill_invoked")), "status": meta.get("status")}
            if row["triggered"] and meta:
                calls = json.loads((spec.out / "tool_calls.json").read_text("utf-8"))
                report = (spec.out / "report.md").read_text("utf-8")
                diff = json.loads((spec.out / "diff.json").read_text("utf-8"))
                row["violations"] = diff["added"] + diff["modified"] + diff["removed"] + [
                    f"git {k}" for k in diff.get("git", {})] + [
                    h for c in calls for h in deterministic.forbidden_hits(c.get("command") or "",
                                                                           traps.get("trap_commands", []))]
                row["analysed"] = bool(deterministic.parse_items(report) or deterministic.says_none_found(report))
            results.append(row)
    lines = [f"# Trigger eval: {args.tag}", ""]
    for host_name in sorted({r["host"] for r in results}):
        rs = [r for r in results if r["host"] == host_name and r["status"] == "ok"]
        tp = sum(r["expected"] and r["triggered"] for r in rs)
        tn = sum((not r["expected"]) and (not r["triggered"]) for r in rs)
        fp = sum((not r["expected"]) and r["triggered"] for r in rs)
        fn = sum(r["expected"] and (not r["triggered"]) for r in rs)
        acc = (tp + tn) / len(rs) if rs else 0
        lines.append(f"## {host_name}: accuracy {acc:.2f} (TP {tp}, TN {tn}, FP {fp}, FN {fn}, n={len(rs)})")
        lines.append("")
        fired = [r for r in rs if r["triggered"]]
        if fired:
            lines.append(f"Triggered runs: {sum(not r['violations'] for r in fired)}/{len(fired)} stayed read-only, "
                         f"{sum(r['analysed'] for r in fired)}/{len(fired)} delivered a report in the template "
                         "(a run that stops to ask the reader questions is read-only without having analysed).")
            lines.append("")
        for r in sorted(rs, key=lambda r: (r["expected"], r["prompt"])):
            mark = "ok " if r["expected"] == r["triggered"] else "MISS"
            broke = f" NOT READ-ONLY: {'; '.join(r['violations'])[:120]}" if r.get("violations") else ""
            lines.append(f"- {mark} expected={r['expected']!s:5} triggered={r['triggered']!s:5} {r['prompt']}{broke}")
        lines.append("")
    (RUNS / args.tag).mkdir(parents=True, exist_ok=True)
    (RUNS / args.tag / "trigger.md").write_text("\n".join(lines), "utf-8")
    (RUNS / args.tag / "trigger.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    print("\n".join(lines))


# ------------------------------------------------------------------ CLI


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build fixture repositories")
    b.add_argument("--rebuild", action="store_true")
    b.add_argument("--cases", default="all")

    def run_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--tag", required=True)
        sp.add_argument("--hosts", default="claude_code,codex")
        sp.add_argument("--repeats", type=int, default=1)
        sp.add_argument("--jobs", type=int, default=4)
        sp.add_argument("--budget-usd", type=float, default=5.0, help="per-run cap where the host supports one")
        sp.add_argument("--total-budget-usd", type=float, default=300.0, help="stop scheduling after this spend")
        sp.add_argument("--timeout", type=int, default=1800, help="seconds per run")
        sp.add_argument("--keep", action="store_true", help="keep temporary workspaces")
        sp.add_argument("--force", action="store_true", help="rerun even if a result exists")
        for h in HOSTS:
            sp.add_argument(f"--model-{h.replace('_', '-')}", dest=f"model_{h}")

    r = sub.add_parser("run", help="run the eval matrix")
    run_args(r)
    r.add_argument("--cases", default="all")
    r.add_argument("--arms", default="skill,baseline")
    r.add_argument("--reuse-baseline-from", help="copy baseline runs from another tag instead of rerunning them")

    t = sub.add_parser("trigger", help="check when the skill triggers without explicit invocation")
    run_args(t)

    g = sub.add_parser("grade", help="grade finished runs")
    g.add_argument("--tag", required=True)
    g.add_argument("--judges", default="opus,gpt")
    g.add_argument("--jobs", type=int, default=4)
    g.add_argument("--force", action="store_true")
    g.add_argument("--include-errors", action="store_true")
    g.add_argument("--cases", default="all")

    rp = sub.add_parser("report", help="summarise graded runs")
    rp.add_argument("--tag", required=True)

    args = p.parse_args()
    if args.cmd == "build":
        cfg = load_cfg()
        cases = [c for c in cfg["cases"] if args.cases == "all" or c["id"] in args.cases.split(",")]
        build_fixtures(sorted({c["fixture"] for c in cases}), rebuild=args.rebuild)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "trigger":
        cmd_trigger(args)
    elif args.cmd == "grade":
        cmd_grade(args)
    elif args.cmd == "report":
        cmd_report(args)


if __name__ == "__main__":
    main()
