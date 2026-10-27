"""Turn raw usage into a verdict: where tokens went, what they bought, what to change."""
from __future__ import annotations

import json
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import pricing, store
from .store import Event


@dataclass
class Finding:
    id: str
    title: str
    severity: str            # high | medium | low | info
    savings_usd: float       # realistic avoidable spend over the analysed period
    evidence: str
    fix: str
    scope: str = ""          # project / model / tool the finding is about


@dataclass
class ProjectRow:
    project: str
    cost: float = 0.0
    tokens: int = 0
    sessions: int = 0
    turns: int = 0
    first: str = ""
    last: str = ""
    clients: str = ""
    top_model: str = ""
    cache_hit: float = 0.0
    commits: int | None = None
    lines_changed: int | None = None
    cost_per_commit: float | None = None
    declared_value: float = 0.0
    avoidable: float = 0.0
    score: int | None = None
    verdict: str = ""


@dataclass
class Report:
    generated: str
    period_start: str
    period_end: str
    total_cost: float
    total_tokens: int
    unpriced_events: int
    by_client: dict
    by_model: dict
    by_tag: dict
    by_day: dict
    token_mix: dict
    projects: list
    tools: list
    findings: list
    sessions_top: list
    notes: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


# ------------------------------------------------------------------ helpers

def _dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _c(e: Event) -> float:
    return e.cost or 0.0


def _ctx(e: Event) -> int:
    return e.input + e.cache_read + e.cache_write


def _git(ws: str, *args) -> str:
    try:
        return subprocess.run(["git", "-C", ws, *args], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def git_commits(ws: str, since: datetime, until: datetime, author: str | None = None) -> list[tuple[datetime, int]]:
    """[(commit time, lines changed)] for commits by `author` (default: repo's user.email)."""
    if not ws or not Path(ws).exists():
        return []
    top = _git(ws, "rev-parse", "--show-toplevel").strip()
    if not top:
        return []
    author = author or _git(top, "config", "user.email").strip()
    args = ["log", "--all", "--no-merges", f"--since={since.isoformat()}", f"--until={until.isoformat()}",
            "--numstat", "--format=@@%cI"]
    if author:
        args.append(f"--author={author}")
    out, commits, cur, lines = _git(top, *args), [], None, 0
    for ln in out.splitlines():
        if ln.startswith("@@"):
            if cur:
                commits.append((cur, lines))
            cur, lines = _dt(ln[2:]), 0
        elif ln.strip():
            a, d, *_ = ln.split("\t") + ["", ""]
            lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    if cur:
        commits.append((cur, lines))
    return commits


def load_outcomes(conn=None) -> dict[str, float]:
    conn = conn or store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS outcomes (project TEXT, date TEXT, note TEXT, value_usd REAL)")
    vals = defaultdict(float)
    for r in conn.execute("SELECT project, value_usd FROM outcomes"):
        vals[r[0]] += r[1] or 0
    return vals


# ------------------------------------------------------------------ main analysis

def analyze(events: list[Event], use_git: bool = True, author: str | None = None) -> Report:
    events = [e for e in events if e.total > 0]
    timed = [e for e in events if e.tag != "aggregate" and _dt(e.ts)]
    start = min((e.ts for e in timed), default="")
    end = max((e.ts for e in timed), default="")
    total = sum(_c(e) for e in events)

    def roll(key):
        d = defaultdict(lambda: [0.0, 0])
        for e in events:
            k = key(e) or "(none)"
            d[k][0] += _c(e); d[k][1] += e.total
        return {k: {"cost": round(v[0], 4), "tokens": v[1]}
                for k, v in sorted(d.items(), key=lambda x: -x[1][0])}

    by_day = defaultdict(float)
    for e in timed:
        by_day[e.ts[:10]] += _c(e)

    mix = {k: sum(getattr(e, k) for e in events) for k in ("input", "cache_read", "cache_write", "output", "reasoning")}

    # ---- sessions
    sessions: dict[str, list[Event]] = defaultdict(list)
    for e in timed:
        sessions[e.session].append(e)
    for s in sessions.values():
        s.sort(key=lambda e: e.ts)

    findings: list[Finding] = []
    avoid_by_project: dict[str, float] = defaultdict(float)

    def add(f: Finding, project: str = ""):
        findings.append(f)
        if project:
            avoid_by_project[project] += f.savings_usd

    # ---- F1 cache misses, per project x model
    grp = defaultdict(list)
    for e in events:
        if e.provider in ("anthropic", "openai", "google"):
            grp[(e.project, e.model)].append(e)
    for (proj, model), es in grp.items():
        p = pricing.lookup(model)
        if not p:
            continue
        inp = sum(e.input for e in es); cr = sum(e.cache_read for e in es); cw = sum(e.cache_write for e in es)
        denom = inp + cr + cw
        if denom < 2_000_000:
            continue
        hit = cr / denom
        if hit < 0.5:
            rate_gap = p["in"] - (p.get("cr") if p.get("cr") is not None else p["in"])
            save = inp * rate_gap * 0.5
            if save >= 1:
                add(Finding("cache-miss", f"{proj}: only {hit:.0%} of input served from cache on {model}",
                            "high" if save > 25 else "medium", round(save, 2),
                            f"{inp:,} uncached input tokens vs {cr:,} cache reads.",
                            "Put stable content (system prompt, tool defs, docs) first and unchanged; "
                            "add cache_control breakpoints (Anthropic) or keep prefixes byte-identical "
                            "(OpenAI/Gemini implicit caching). Avoid timestamps or IDs at the top of prompts.",
                            proj), proj)

    # ---- F2 cache writes that never pay back
    for (proj, model), es in grp.items():
        p = pricing.lookup(model)
        cw = sum(e.cache_write for e in es); cr = sum(e.cache_read for e in es)
        if not p or cw < 500_000 or p.get("cw") is None:
            continue
        if cr / cw < 2:
            waste = cw * (p["cw"] - p["in"]) + max(0, cw - cr) * p["in"] * 0.5
            if waste >= 1:
                add(Finding("cache-churn", f"{proj}: cache is written but rarely re-read ({cr / cw:.1f}x)",
                            "medium", round(waste, 2),
                            f"{cw:,} tokens written to cache, {cr:,} read back on {model}.",
                            "Cache writes cost more than plain input. Either the prefix keeps changing "
                            "(edits to CLAUDE.md, tool lists, MCP servers toggling) or sessions are too short "
                            "to reuse it. Stabilise the prefix or drop cache_control on one-shot calls.",
                            proj), proj)

    # ---- F3 premium model on small turns
    overkill = defaultdict(lambda: [0.0, 0.0, 0, ""])
    for e in timed:
        alt = pricing.cheaper_alternative(e.model)
        if not alt or e.output > 400 or _ctx(e) > 30_000 or e.tools:
            continue
        alt_cost = pricing.cost(alt, e.input, e.output, e.cache_read, e.cache_write) or 0
        o = overkill[(e.project, e.model)]
        o[0] += _c(e); o[1] += alt_cost; o[2] += 1; o[3] = alt
    for (proj, model), (c, ac, n, alt) in overkill.items():
        save = (c - ac) * 0.6
        if n >= 20 and save >= 1:
            add(Finding("model-overkill", f"{proj}: {n} small turns on {model}",
                        "medium", round(save, 2),
                        f"Turns with <400 output tokens, small context and no tool calls cost ${c:.2f}; "
                        f"the same tokens on {alt} cost ${ac:.2f}.",
                        f"Route short Q&A, classification and summarising to {alt}. In Claude Code use "
                        "/model for quick questions or set a cheaper subagent model; in apps add a router.",
                        proj), proj)

    # ---- F4 context bloat per session
    bloat = defaultdict(lambda: [0.0, 0])
    for sid, es in sessions.items():
        if len(es) < 10:
            continue
        med = statistics.median(_ctx(e) for e in es)
        if med > 120_000:
            proj = es[0].project
            p = pricing.lookup(es[0].model) or {}
            crp = p.get("cr") or p.get("in") or 0
            excess = sum(max(0, _ctx(e) - 60_000) for e in es) * crp * 0.5
            bloat[proj][0] += excess; bloat[proj][1] += 1
    for proj, (save, n) in bloat.items():
        if save >= 1:
            add(Finding("context-bloat", f"{proj}: {n} long sessions carried >120k tokens of context per turn",
                        "high" if save > 25 else "medium", round(save, 2),
                        "Every turn re-reads the whole conversation. Past ~60k tokens most of that is stale.",
                        "Start a fresh session per task (/clear), /compact at natural breakpoints, keep "
                        "CLAUDE.md short, and push exploration into subagents so their reads never enter "
                        "the main context.", proj), proj)

    # ---- F5 tool output carry cost (the unique one)
    tool_added = defaultdict(int); tool_carry = defaultdict(float); tool_calls = Counter()
    for sid, es in sessions.items():
        n = len(es)
        for i, e in enumerate(es):
            for t in filter(None, e.tools.split(",")):
                tool_calls[t] += 1
            if not e.tool_ctx:
                continue
            p = pricing.lookup(e.model) or {}
            crp = p.get("cr") or p.get("in") or 0
            cwp = p.get("cw") or p.get("in") or 0
            for tool, toks in json.loads(e.tool_ctx).items():
                tool_added[tool] += toks
                tool_carry[tool] += toks * cwp + toks * (n - i - 1) * crp
    tools = sorted(({"tool": t, "calls": tool_calls[t], "tokens_added": tool_added[t],
                     "carry_cost": round(tool_carry[t], 2)} for t in set(tool_added) | set(tool_calls)),
                   key=lambda r: -r["carry_cost"])
    if tools and tools[0]["carry_cost"] >= 2:
        t = tools[0]
        add(Finding("tool-carry", f"Tool output from {t['tool']} is your biggest hidden cost",
                    "high" if t["carry_cost"] > 25 else "medium", round(t["carry_cost"] * 0.4, 2),
                    f"{t['calls']:,} calls pushed ~{t['tokens_added']:,} tokens into context; re-reading "
                    f"them on later turns cost about ${t['carry_cost']:.2f}.",
                    _tool_fix(t["tool"]), t["tool"]))

    # ---- git outcomes + F6 sessions with no shipped commit
    outcomes = load_outcomes()
    proj_rows: dict[str, ProjectRow] = {}
    proj_events = defaultdict(list)
    for e in events:
        proj_events[e.project].append(e)
    commit_cache: dict[str, list] = {}
    for proj, es in proj_events.items():
        row = ProjectRow(project=proj)
        row.cost = sum(_c(e) for e in es); row.tokens = sum(e.total for e in es)
        row.sessions = len({e.session for e in es}); row.turns = len(es)
        ts = sorted(e.ts for e in es if e.tag != "aggregate" and e.ts)
        row.first, row.last = (ts[0][:10], ts[-1][:10]) if ts else ("", "")
        row.clients = ",".join(sorted({e.client for e in es}))
        mc = Counter()
        for e in es:
            mc[e.model] += _c(e)
        row.top_model = mc.most_common(1)[0][0] if mc else ""
        den = sum(_ctx(e) for e in es)
        row.cache_hit = round(sum(e.cache_read for e in es) / den, 3) if den else 0
        row.declared_value = outcomes.get(proj, 0.0)
        ws = Counter(e.workspace for e in es if e.workspace).most_common(1)
        if use_git and ws and ts:
            s, u = _dt(ts[0]) - timedelta(hours=1), _dt(ts[-1]) + timedelta(days=1)
            commits = git_commits(ws[0][0], s, u, author)
            if commits or Path(ws[0][0]).exists():
                commit_cache[proj] = commits
                row.commits = len(commits); row.lines_changed = sum(l for _, l in commits)
                row.cost_per_commit = round(row.cost / row.commits, 2) if row.commits else None
        row.avoidable = round(min(avoid_by_project.get(proj, 0), row.cost), 2)
        proj_rows[proj] = row

    idle_cost, idle_n, idle_examples = 0.0, 0, []
    for sid, es in sessions.items():
        proj = es[0].project
        if proj not in commit_cache:
            continue
        cost = sum(_c(e) for e in es)
        if cost < 2:
            continue
        s, u = _dt(es[0].ts), _dt(es[-1].ts) + timedelta(hours=3)
        if not any(s <= t <= u for t, _ in commit_cache[proj]):
            idle_cost += cost; idle_n += 1
            idle_examples.append(f"{proj} {es[0].ts[:10]} ${cost:.2f}")
    if idle_n:
        add(Finding("no-outcome", f"{idle_n} sessions (${idle_cost:.2f}) ended with no commit",
                    "medium" if idle_cost > 20 else "low", round(idle_cost * 0.3, 2),
                    "Examples: " + "; ".join(sorted(idle_examples, key=lambda x: -float(x.split('$')[-1]))[:4]),
                    "Not all of this is waste (research, reviews, planning). Tag real outcomes with "
                    "`tokenaudit outcome add`, and for sessions that drift, write the goal in the first "
                    "message and stop when it is met."))

    # ---- F7 reasoning-heavy output
    for (proj, model), es in grp.items():
        out = sum(e.output for e in es); rs = sum(e.reasoning for e in es)
        p = pricing.lookup(model)
        if p and out > 200_000 and rs / out > 0.7:
            save = rs * p["out"] * 0.3
            if save >= 1:
                add(Finding("reasoning-heavy", f"{proj}: {rs / out:.0%} of {model} output is hidden reasoning",
                            "low", round(save, 2), f"{rs:,} reasoning tokens of {out:,} output.",
                            "Lower reasoning effort for routine steps (reasoning_effort / thinking budget), "
                            "keep high effort for planning and debugging.", proj), proj)

    # ---- score projects
    cpcs = [r.cost_per_commit for r in proj_rows.values() if r.cost_per_commit]
    med_cpc = statistics.median(cpcs) if cpcs else None
    for r in proj_rows.values():
        eff = 50 * (1 - min(1, r.avoidable / r.cost)) if r.cost else 50
        if r.declared_value:
            roi = r.declared_value / max(r.cost, 0.01)
            out = 50 if roi >= 5 else 35 if roi >= 1 else 15
        elif r.commits is None:
            r.score = None; r.verdict = f"No outcome data (efficiency {int(eff * 2)}/100)"
            continue
        elif r.commits == 0:
            out = 0 if r.cost > 5 else 25
        else:
            ratio = r.cost_per_commit / med_cpc if med_cpc else 1
            out = 50 if ratio <= 0.75 else 35 if ratio <= 1.5 else 20 if ratio <= 3 else 5
        r.score = int(round(eff + out))
        r.verdict = "Worth it" if r.score >= 70 else "Review" if r.score >= 45 else "Likely waste"

    projects = sorted((asdict(r) for r in proj_rows.values()), key=lambda r: -r["cost"])
    top_sessions = sorted(({"session": sid[:8], "project": es[0].project, "client": es[0].client,
                            "date": es[0].ts[:10], "turns": len(es),
                            "cost": round(sum(_c(e) for e in es), 2),
                            "peak_context": max(_ctx(e) for e in es)}
                           for sid, es in sessions.items()), key=lambda r: -r["cost"])[:10]
    sev = {"high": 0, "medium": 1, "low": 2, "info": 3}
    findings.sort(key=lambda f: (sev[f.severity], -f.savings_usd))

    notes = []
    unpriced = sum(1 for e in events if e.cost is None)
    if unpriced:
        notes.append(f"{unpriced} events had no price for their model; run `tokenaudit prices --refresh`.")
    if any(e.tag == "aggregate" for e in events):
        notes.append("Non-Claude CLI agents come from tokscale as all-time totals per workspace and model; "
                     "they count in totals but not in daily or per-session views.")
    notes.append("Costs are list-price estimates. Subscription plans (Claude Max, ChatGPT Pro) bill a flat fee; "
                 "read the dollar figures as value consumed, not cash spent.")

    return Report(
        generated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        period_start=start[:10], period_end=end[:10], total_cost=round(total, 2),
        total_tokens=sum(e.total for e in events), unpriced_events=unpriced,
        by_client=roll(lambda e: e.client), by_model=roll(lambda e: e.model),
        by_tag=roll(lambda e: e.tag if e.tag != "aggregate" else ""),
        by_day={k: round(v, 2) for k, v in sorted(by_day.items())}, token_mix=mix,
        projects=projects, tools=tools[:15], findings=[asdict(f) for f in findings],
        sessions_top=top_sessions, notes=notes)


def _tool_fix(tool: str) -> str:
    t = tool.lower()
    if t in ("read", "view", "read_file"):
        return ("Read narrower: grep or glob first, then read line ranges (offset/limit) instead of whole "
                "files. Send broad codebase exploration to a subagent so the raw text stays out of the "
                "main session.")
    if t in ("bash", "shell", "run_command"):
        return ("Pipe noisy commands through `| tail -50` or `| head`, run tests with quiet flags, and "
                "redirect build logs to a file you grep instead of dumping them into context.")
    if t.startswith("mcp__") or "fetch" in t or "search" in t:
        return ("This tool returns large payloads. Ask for fewer fields or results, or wrap it in a "
                "subagent that returns only the summary.")
    return "Return less text from this tool, or call it from a subagent that hands back only the summary."
