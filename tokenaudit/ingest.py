"""Pull usage that already sits on disk.

* Claude Code: parsed natively, turn by turn, including which tools pushed how much
  text into the context window. This is the detail other trackers throw away.
* Everything else (Codex CLI, Gemini CLI, Cursor, OpenCode, Amp, 40+ agents):
  delegated to tokscale (MIT, github.com/junhoyeo/tokscale), which already does the
  hard log-format work. We import its per-workspace JSON and add the analysis layer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from . import pricing, store


# ------------------------------------------------------------------ Claude Code

def claude_roots() -> list[Path]:
    roots = []
    for env in ("CLAUDE_CONFIG_DIR",):
        for p in filter(None, os.environ.get(env, "").split(",")):
            roots.append(Path(p).expanduser() / "projects")
    roots += [Path.home() / ".claude" / "projects", Path.home() / ".config" / "claude" / "projects"]
    seen, out = set(), []
    for r in roots:
        if r.exists() and r.resolve() not in seen:
            seen.add(r.resolve()); out.append(r)
    return out


@lru_cache(maxsize=512)
def project_of(cwd: str) -> str:
    if not cwd:
        return "unknown"
    p = Path(cwd)
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            # .claude/worktrees/<name> belongs to the outer repo
            if ".claude" in parent.parts:
                continue
            return parent.name
    return p.name or "unknown"


def _text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    n += len(c.get("text", ""))
                elif c.get("type") == "image":
                    n += 6000   # ~1.5k tokens per image, rough
                else:
                    n += len(json.dumps(c))
        return n
    return 0


def parse_claude_file(path: Path) -> Iterator[store.Event]:
    events: dict[str, store.Event] = {}
    tool_owner: dict[str, tuple[str, str]] = {}          # tool_use_id -> (event key, tool name)
    tool_ctx: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cwd = ""
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"usage"' not in line and '"tool_result"' not in line and '"cwd"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            cwd = d.get("cwd") or cwd
            msg = d.get("message") or {}
            if d.get("type") == "assistant" and isinstance(msg.get("usage"), dict):
                model = msg.get("model") or ""
                if model.startswith("<"):           # <synthetic> placeholder turns
                    continue
                u = msg["usage"]
                key = f"cc:{msg.get('id')}:{d.get('requestId')}"
                tools = [c.get("name", "") for c in msg.get("content") or []
                         if isinstance(c, dict) and c.get("type") == "tool_use"]
                for c in msg.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tool_owner[c.get("id")] = (key, c.get("name", "?"))
                prev = events.get(key)
                if prev and prev.tools:
                    tools = sorted(set(prev.tools.split(",")) | set(tools))
                inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
                cr, cw = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
                c = d.get("costUSD")
                if c is None:
                    c = pricing.cost(model, inp, out, cr, cw)
                events[key] = store.Event(
                    dedup_key=key, ts=d.get("timestamp") or "", source="claude-code",
                    client="claude-code", provider="anthropic", model=model,
                    project=project_of(cwd), workspace=cwd, session=d.get("sessionId") or path.stem,
                    is_subagent=1 if (d.get("isSidechain") or "subagents" in path.parts) else 0,
                    input=inp, output=out, cache_read=cr, cache_write=cw, cost=c,
                    tools=",".join(t for t in tools if t))
            elif d.get("type") == "user" and isinstance(msg.get("content"), list):
                for c in msg["content"]:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        owner = tool_owner.get(c.get("tool_use_id"))
                        if owner:
                            tool_ctx[owner[0]][owner[1]] += _text_len(c.get("content")) // 4
    for k, e in events.items():
        if k in tool_ctx:
            e.tool_ctx = json.dumps(dict(tool_ctx[k]))
        yield e


def ingest_claude_code(roots: list[Path] | None = None) -> int:
    n = 0
    for root in roots or claude_roots():
        for f in root.rglob("*.jsonl"):
            n += store.upsert(list(parse_claude_file(f)))
    return n


# ------------------------------------------------------------------ tokscale bridge

def _tokscale_cmd() -> list[str] | None:
    if os.environ.get("TOKENAUDIT_TOKSCALE"):
        return os.environ["TOKENAUDIT_TOKSCALE"].split()
    if shutil.which("tokscale"):
        return ["tokscale"]
    if shutil.which("npx"):
        return ["npx", "-y", "tokscale@latest"]
    if shutil.which("bunx"):
        return ["bunx", "tokscale@latest"]
    return None


def _run_tokscale(args: list[str], timeout: int = 600) -> dict:
    cmd = _tokscale_cmd()
    if not cmd:
        raise RuntimeError("tokscale not found. Install Node or Bun, or `npm i -g tokscale`.")
    out = subprocess.run(cmd + args + ["--no-spinner"], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        # older tokscale builds lack --no-spinner
        out = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-400:])
    txt = out.stdout
    return json.loads(txt[txt.index("{"):])


def rows_from_tokscale(data: dict, client_hint: str = "") -> list[store.Event]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    evs = []
    for r in data.get("entries", []):
        client = r.get("client") or client_hint or "unknown"
        if client in ("claude", "claude-code"):
            continue   # parsed natively with more detail
        ws = r.get("workspaceKey") or ""
        label = r.get("workspaceLabel") or (Path(ws).name if ws else "unattributed")
        model = r.get("model") or ""
        inp, out = int(r.get("input") or 0), int(r.get("output") or 0)
        cr, cw = int(r.get("cacheRead") or 0), int(r.get("cacheWrite") or 0)
        c = r.get("cost")
        if c is None:
            c = pricing.cost(model, inp, out, cr, cw)
        evs.append(store.Event(
            dedup_key=f"ts:{client}:{ws}:{model}", ts=now, source="tokscale", client=client,
            provider=r.get("provider") or "", model=model, project=label.split(" ⑃")[0],
            workspace=ws, session=f"{client}-aggregate", input=inp, output=out,
            cache_read=cr, cache_write=cw, reasoning=int(r.get("reasoning") or 0),
            cost=c, tag="aggregate"))
    return evs


def ingest_tokscale(clients: list[str] | None = None) -> int:
    """Import all non-Claude agents, one row per (client, workspace, model), all-time totals."""
    if clients is None:
        overview = _run_tokscale(["models", "--json", "--group-by", "client,model"])
        clients = sorted({e.get("client") for e in overview.get("entries", [])
                          if e.get("client") and e.get("client") not in ("claude", "claude-code")})
    n = 0
    for c in clients:
        data = _run_tokscale(["models", "--json", "--group-by", "workspace,model",
                              "--merge-worktrees", "--client", c])
        n += store.upsert(rows_from_tokscale(data, client_hint=c))
    return n
