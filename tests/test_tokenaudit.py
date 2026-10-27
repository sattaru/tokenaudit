import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import httpx2 as httpx  # newer OpenAI/Anthropic SDKs moved to httpx2
except ImportError:
    import httpx
import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENAUDIT_DB", str(tmp_path / "usage.db"))
    monkeypatch.setenv("TOKENAUDIT_PROJECT", "test-app")
    import tokenaudit.store as st
    st._conn = None
    yield
    st._conn = None


# ------------------------------------------------------------- SDK capture

def _openai_client():
    import openai

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={
                "id": "c1", "object": "chat.completion", "created": 1, "model": "gpt-5-mini-2025-08-07",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
                          "prompt_tokens_details": {"cached_tokens": 1000},
                          "completion_tokens_details": {"reasoning_tokens": 40}}})
        return httpx.Response(200, json={
            "id": "r1", "object": "response", "created_at": 1, "model": body["model"], "status": "completed",
            "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 500, "output_tokens": 50, "total_tokens": 550,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}})
    return openai.OpenAI(api_key="x", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def _anthropic_client():
    import anthropic

    def handler(req):
        return httpx.Response(200, json={
            "id": "m1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-5-20250929",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 30, "output_tokens": 200, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 1000}})
    return anthropic.Anthropic(api_key="x", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_sdk_capture_all_three(monkeypatch):
    import tokenaudit
    from tokenaudit import store
    tokenaudit.install()

    with tokenaudit.tag("resume-rewrite"):
        _openai_client().chat.completions.create(model="gpt-5-mini", messages=[{"role": "user", "content": "hi"}])
    _openai_client().responses.create(model="gpt-5", input="hi")
    _anthropic_client().messages.create(model="claude-sonnet-4-5", max_tokens=10,
                                        messages=[{"role": "user", "content": "hi"}])

    from google.genai import models as gm, types, Client
    fake = types.GenerateContentResponse(model_version="gemini-2.5-flash", usage_metadata=types.GenerateContentResponseUsageMetadata(
        prompt_token_count=4000, cached_content_token_count=3000, candidates_token_count=100, thoughts_token_count=50))
    monkeypatch.setattr(gm.Models, "_generate_content", lambda self, **kw: fake)
    Client(api_key="x").models.generate_content(model="gemini-2.5-flash", contents="hi")

    ev = {e.provider + ":" + e.model: e for e in store.load()}
    oa = ev["openai:gpt-5-mini-2025-08-07"]
    assert (oa.input, oa.cache_read, oa.output, oa.reasoning, oa.tag) == (200, 1000, 80, 40, "resume-rewrite")
    assert oa.cost and oa.cost > 0 and oa.project == "test-app"
    assert ev["openai:gpt-5"].input == 500
    an = ev["anthropic:claude-sonnet-4-5-20250929"]
    assert (an.input, an.cache_read, an.cache_write, an.output) == (30, 9000, 1000, 200)
    # 30*3e-6 + 200*15e-6 + 9000*0.3e-6 + 1000*3.75e-6
    assert an.cost == pytest.approx(0.00954, rel=1e-3)
    ge = ev["google:gemini-2.5-flash"]
    assert (ge.input, ge.cache_read, ge.output, ge.reasoning) == (1000, 3000, 150, 50)


def test_tracking_never_breaks_app(monkeypatch):
    import tokenaudit
    from tokenaudit import store
    tokenaudit.install()
    monkeypatch.setattr(store, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    r = _anthropic_client().messages.create(model="claude-sonnet-4-5", max_tokens=10,
                                            messages=[{"role": "user", "content": "hi"}])
    assert r.content[0].text == "ok"


def test_anthropic_stream_usage():
    from tokenaudit.track import _StreamProxy, _harvest_anthropic
    from tokenaudit import store
    chunks = [{"type": "message_start", "message": {"model": "claude-haiku-4-5",
               "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 500}}},
              {"type": "content_block_delta"},
              {"type": "message_delta", "usage": {"output_tokens": 77}}]
    list(_StreamProxy(iter(chunks), "anthropic", "claude-haiku-4-5", 0, _harvest_anthropic))
    e = store.load()[0]
    assert (e.input, e.cache_read, e.output) == (10, 500, 77)


# ------------------------------------------------------------- pricing

def test_pricing_normalisation():
    from tokenaudit import pricing
    assert pricing.lookup("claude-sonnet-4-5-20250929")["in"] == pytest.approx(3e-6)
    assert pricing.lookup("anthropic/claude-haiku-4-5")["out"] == pytest.approx(5e-6)
    assert pricing.lookup("models/gemini-2.5-pro") is not None
    assert pricing.lookup("totally-made-up-model") is None
    assert pricing.cheaper_alternative("claude-opus-4-5-20251101") == "claude-sonnet-4-5"


# ------------------------------------------------------------- Claude Code + analysis end to end

def _git(cwd, *a, when=None):
    env = dict(os.environ, GIT_AUTHOR_DATE=when or "", GIT_COMMITTER_DATE=when or "")
    if not when:
        env.pop("GIT_AUTHOR_DATE"); env.pop("GIT_COMMITTER_DATE")
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, env=env)


def make_claude_logs(tmp_path):
    """Two repos: 'shipper' commits after its sessions, 'spinner' burns tokens and never commits."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    root = tmp_path / "claude" / "projects"
    repos = {}
    for name in ("shipper", "spinner"):
        repo = tmp_path / "code" / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q"); _git(repo, "config", "user.email", "me@x.dev"); _git(repo, "config", "user.name", "me")
        (repo / "README.md").write_text("x\n"); _git(repo, "add", "."); _git(repo, "commit", "-qm", "init",
                                                                           when=(now - timedelta(days=30)).isoformat())
        repos[name] = repo

    for name, repo in repos.items():
        for s in range(3):
            sid = f"{name}-{s}"
            start = now - timedelta(days=5 - s)
            lines = []
            for t in range(40):
                ts = (start + timedelta(minutes=t)).isoformat().replace("+00:00", "Z")
                big = name == "spinner"
                tid = f"tu_{sid}_{t}"
                lines.append({"type": "assistant", "cwd": str(repo), "sessionId": sid, "timestamp": ts,
                              "requestId": f"req_{sid}_{t}", "isSidechain": t % 10 == 0,
                              "message": {"id": f"msg_{sid}_{t}", "model": "claude-opus-4-5-20251101",
                                          "content": [{"type": "tool_use", "id": tid, "name": "Read" if t % 2 else "Bash"}],
                                          "usage": {"input_tokens": 200000 if big else 20,
                                                    "output_tokens": 300,
                                                    "cache_read_input_tokens": (100000 if big else 30000) + t * 2000,
                                                    "cache_creation_input_tokens": 2000}}})
                # duplicate streaming line for the same message: must be counted once
                lines.append(dict(lines[-1]))
                lines.append({"type": "user", "cwd": str(repo), "sessionId": sid, "timestamp": ts,
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": tid, "content": "x" * (40000 if big else 4000)}]}})
            d = root / f"-code-{name}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(l) for l in lines))
            if name == "shipper":
                (repo / f"f{s}.py").write_text("print(1)\n" * 20)
                _git(repo, "add", "."); _git(repo, "commit", "-qm", f"feat {s}",
                                             when=(start + timedelta(minutes=45)).isoformat())
    return root


def test_claude_code_ingest_and_review(tmp_path, capsys):
    from tokenaudit import ingest, store, analyze, report
    root = make_claude_logs(tmp_path)
    n = ingest.ingest_claude_code([root])
    evs = store.load()
    assert len(evs) == 2 * 3 * 40, "streaming duplicates must collapse to one event per message"
    assert n >= len(evs)
    e = evs[0]
    assert e.client == "claude-code" and e.project in ("shipper", "spinner") and e.cost > 0
    assert json.loads(e.tool_ctx)
    assert sum(x.is_subagent for x in evs) == 2 * 3 * 4

    r = analyze.analyze(evs, use_git=True)
    proj = {p["project"]: p for p in r.projects}
    assert proj["shipper"]["commits"] == 3 and proj["spinner"]["commits"] == 0
    assert proj["spinner"]["verdict"] == "Likely waste"
    assert proj["shipper"]["score"] > proj["spinner"]["score"]
    ids = {f["id"] for f in r.findings}
    assert {"cache-miss", "context-bloat", "tool-carry", "no-outcome"} <= ids
    assert r.tools[0]["tool"] in ("Read", "Bash")

    md = report.markdown(report.redact(r))
    assert "spinner" not in md and "shipper" not in md and str(tmp_path) not in md
    html = report.html_page(r)
    assert "<svg" in html and "Was it worth it?" in html
    print(report.terminal(r))
    assert "PROJECTS" in capsys.readouterr().out


def test_tokscale_rows():
    from tokenaudit.ingest import rows_from_tokscale
    data = {"groupBy": "workspace,model", "entries": [
        {"workspaceKey": "/home/me/api", "workspaceLabel": "api", "model": "gpt-5-codex", "provider": "openai",
         "input": 1_000_000, "output": 50_000, "cacheRead": 4_000_000, "cacheWrite": 0, "reasoning": 30_000,
         "cost": 3.1},
        {"workspaceKey": "", "workspaceLabel": None, "model": "gemini-2.5-pro", "input": 10, "output": 5,
         "cacheRead": 0, "cacheWrite": 0}]}
    rows = rows_from_tokscale(data, client_hint="codex")
    assert rows[0].project == "api" and rows[0].cost == 3.1 and rows[0].client == "codex"
    assert rows[1].project == "unattributed" and rows[1].cost is not None


def test_cli_review(tmp_path, monkeypatch):
    from tokenaudit import cli
    root = make_claude_logs(tmp_path)
    out = tmp_path / "out"
    cli.main(["review", "--claude-dir", str(root), "--no-tokscale", "--out", str(out), "--public"])
    assert (out / "token-audit.md").exists() and (out / "token-audit.html").exists()
    data = json.loads((out / "token-audit.json").read_text())
    assert data["total_cost"] > 0 and all(p["project"].startswith("project-") for p in data["projects"])
