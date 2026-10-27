"""tokenaudit command line."""
from __future__ import annotations

import argparse
import site
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analyze, ingest, pricing, report, store

PTH_NAME = "tokenaudit_auto.pth"


def cmd_scan(a) -> None:
    pricing.maybe_refresh()
    roots = [Path(p).expanduser() for p in a.claude_dir] if a.claude_dir else None
    n = ingest.ingest_claude_code(roots)
    print(f"Claude Code: {n:,} turns indexed")
    if not a.no_tokscale:
        try:
            m = ingest.ingest_tokscale()
            print(f"Other agents via tokscale: {m:,} workspace/model rows")
        except Exception as e:
            print(f"Other agents skipped ({e}). Install Node/Bun or pass --no-tokscale.")


def cmd_review(a) -> None:
    if not a.skip_scan:
        cmd_scan(a)
    since = None
    if a.days:
        since = (datetime.now(timezone.utc) - timedelta(days=a.days)).isoformat()
    events = store.load(since=since, project=a.project)
    if since:  # aggregate rows have no real timestamp; keep them only for all-time reviews
        events = [e for e in events if e.tag != "aggregate"]
    if not events:
        print("No usage found yet. Run with Claude Code history present, or call tokenaudit.install() in your app.")
        return
    r = analyze.analyze(events, use_git=not a.no_git, author=a.author)
    shown = report.redact(r, keep_projects=a.keep_names) if a.public else r
    print(report.terminal(shown))
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    (out / "token-audit.md").write_text(report.markdown(shown, a.title))
    (out / "token-audit.html").write_text(report.html_page(shown, a.title))
    (out / "token-audit.json").write_text(shown.to_json())
    print(f"\nWrote {out / 'token-audit.md'}, .html and .json" + ("  (public: redacted)" if a.public else ""))


def cmd_outcome(a) -> None:
    conn = store.connect()
    analyze.load_outcomes(conn)
    conn.execute("INSERT INTO outcomes VALUES (?,?,?,?)",
                 (a.project, datetime.now().date().isoformat(), a.note, a.value))
    conn.commit()
    print(f"Recorded outcome for {a.project}: {a.note} (${a.value:,.2f})")


def cmd_prices(a) -> None:
    if a.refresh:
        print(f"Cached {pricing.refresh():,} model prices from LiteLLM")
    for m in a.model or []:
        p = pricing.lookup(m)
        print(m, "→", "unknown" if not p else
              f"in ${p['in'] * 1e6:.3f}/M  out ${p['out'] * 1e6:.3f}/M  "
              f"cache-read ${(p.get('cr') or 0) * 1e6:.3f}/M  cache-write ${(p.get('cw') or 0) * 1e6:.3f}/M")


def _site_dir() -> Path:
    import os
    for d in [*site.getsitepackages(), site.getusersitepackages()]:
        p = Path(d)
        if p.exists() and os.access(p, os.W_OK):
            return p
    p = Path(site.getusersitepackages()); p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_enable(a) -> None:
    p = _site_dir() / PTH_NAME
    p.write_text("import tokenaudit.auto\n")
    print(f"Auto-tracking on for this Python ({p}). Every OpenAI, Anthropic and Gemini SDK call is now logged.\n"
          "Disable per process with TOKENAUDIT_OFF=1, or run `tokenaudit disable`.")


def cmd_disable(a) -> None:
    removed = False
    for d in [site.getusersitepackages(), *site.getsitepackages()]:
        p = Path(d) / PTH_NAME
        if p.exists():
            p.unlink(); removed = True
    print("Auto-tracking off." if removed else "Auto-tracking was not enabled.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="tokenaudit", description="See where your AI tokens go and whether they were worth it.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="index Claude Code logs and other CLI agents")
    s.add_argument("--claude-dir", action="append", help="extra Claude projects dir (repeatable)")
    s.add_argument("--no-tokscale", action="store_true", help="skip Codex/Gemini/Cursor import")
    s.set_defaults(fn=cmd_scan)

    r = sub.add_parser("review", help="scan, analyse and write the report")
    r.add_argument("--days", type=int, help="only the last N days")
    r.add_argument("--project")
    r.add_argument("--out", default="./token-audit")
    r.add_argument("--title", default="Token Audit")
    r.add_argument("--public", action="store_true", help="redact paths, sessions and project names for sharing")
    r.add_argument("--keep-names", action="store_true", help="with --public, keep project names")
    r.add_argument("--no-git", action="store_true", help="skip commit attribution")
    r.add_argument("--author", help="git author to credit (default: each repo's user.email)")
    r.add_argument("--skip-scan", action="store_true")
    r.add_argument("--claude-dir", action="append")
    r.add_argument("--no-tokscale", action="store_true")
    r.set_defaults(fn=cmd_review)

    o = sub.add_parser("outcome", help="record business value for a project")
    osub = o.add_subparsers(dest="ocmd", required=True)
    oa = osub.add_parser("add")
    oa.add_argument("project"); oa.add_argument("note"); oa.add_argument("--value", type=float, default=0.0)
    oa.set_defaults(fn=cmd_outcome)

    p = sub.add_parser("prices", help="refresh or look up model prices")
    p.add_argument("--refresh", action="store_true"); p.add_argument("model", nargs="*")
    p.set_defaults(fn=cmd_prices)

    sub.add_parser("enable", help="auto-track every Python AI SDK call").set_defaults(fn=cmd_enable)
    sub.add_parser("disable").set_defaults(fn=cmd_disable)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
