# tokenaudit

[![tests](https://github.com/sattaru/tokenaudit/actions/workflows/tests.yml/badge.svg)](https://github.com/sattaru/tokenaudit/actions/workflows/tests.yml)

Every token tracker tells you what you spent. tokenaudit tells you whether it was worth it.

It reads your Claude Code history, pulls in Codex CLI, Gemini CLI, Cursor and 40+ other agents through [tokscale](https://github.com/junhoyeo/tokscale), captures every OpenAI, Anthropic and Gemini SDK call your own apps make, and then answers three questions per project:

1. **Where did the tokens go?** By project, model, tool, feature tag and day.
2. **What did they buy?** Spend joined to git commits and lines changed, plus any business value you record.
3. **What should change?** Ranked findings with a dollar estimate and a specific fix.

Everything stays local in `~/.tokenaudit/usage.db`. Nothing is uploaded.

Example output (illustrative numbers):

```
Token audit  2026-09-28 → 2026-10-27
Spend $412.80   Tokens 611.2M
Possible savings: up to $151.30 (37% of spend; findings overlap)

PROJECTS
  imerzn-api                      $188.40  commits   41  cache 92%  Worth it
  resume-engine                   $131.75  commits    3  cache 38%  Likely waste
  infra-scripts                    $54.10  commits   12  cache 88%  Worth it

TOP FINDINGS
  1. [high] resume-engine: only 38% of input served from cache on claude-opus-4-5  (save ~$48.20)
  2. [high] Tool output from Read is your biggest hidden cost  (save ~$31.70)
  3. [medium] 9 sessions ($61.40) ended with no commit  (save ~$18.42)
```

## Install

```bash
pip install git+https://github.com/sattaru/tokenaudit
tokenaudit review                # scan, analyse, write ./token-audit/token-audit.{md,html,json}
```

Node or Bun on the PATH lets it pull non-Claude agents through `npx tokscale`. Without them it still covers Claude Code and your SDK apps.

## Capture your own apps (zero code)

```bash
tokenaudit enable    # adds a one-line .pth hook; every Python process now logs SDK usage
```

The hook does nothing until your code imports `openai`, `anthropic` or `google.genai`, then patches them. Tracking failures never reach your app. Turn it off with `tokenaudit disable` or `TOKENAUDIT_OFF=1`.

Prefer explicit?

```python
import tokenaudit
tokenaudit.install()                         # patch OpenAI, Anthropic, Gemini SDKs

with tokenaudit.tag("resume-rewrite"):       # attribute calls to a feature or endpoint
    client.messages.create(...)
```

Covered: OpenAI `chat.completions.create` and `responses.create`, Anthropic `messages.create`, Gemini `models.generate_content` and `generate_content_stream`, sync and async, streaming included (OpenAI streams get `include_usage` added automatically). Project defaults to the git repo name; override with `TOKENAUDIT_PROJECT`.

## Use it as a skill (Claude, Codex, Gemini)

The skill turns the numbers into a conversation: a holistic review, then a working session on the fixes.

| Agent | Install |
|---|---|
| Claude Code / Claude apps | copy `skills/token-audit/` to `~/.claude/skills/` (or upload it as a skill) |
| Codex CLI | copy `integrations/codex/SKILL.md` to `~/.codex/skills/token-audit/SKILL.md` |
| Gemini CLI | copy `integrations/gemini/` to `~/.gemini/extensions/token-audit/`, then run `/token-audit` |

Then ask: "Where are my tokens going, and was it worth it?"

## What it finds

| Finding | What it means | Typical fix |
|---|---|---|
| `cache-miss` | Large input with a low cache-hit rate | Stable prefix first, `cache_control` breakpoints, no timestamps at the top |
| `cache-churn` | Cache written but rarely read back | Stop editing the prefix mid-session; drop caching on one-shot calls |
| `context-bloat` | Sessions carrying >120k tokens every turn | One task per session, `/clear`, `/compact`, short `CLAUDE.md` |
| `tool-carry` | A tool's output keeps getting re-read on every later turn | Narrow reads, quiet commands, summarising subagents |
| `model-overkill` | Premium model on small, tool-free turns | Route short work to a cheaper model |
| `no-outcome` | Expensive sessions with no commit afterwards | Set a goal up front; record non-code value with `tokenaudit outcome add` |
| `reasoning-heavy` | Most output is hidden reasoning | Lower reasoning effort for routine steps |

**Tool carry cost** is the one you will not find elsewhere. When Claude Code reads a 2,000-line file, that text sits in context and is billed again (at cache rates) on every later turn of the session. tokenaudit traces each tool result back to the tool that produced it and prices the re-reads.

## Worth-it score

Each project gets 0 to 100: half from efficiency (share of spend not flagged as avoidable), half from outcome (cost per commit against your own median, or declared value / spend if you record outcomes). 70+ is "Worth it", 45 to 69 "Review", below 45 "Likely waste". Projects outside a git repo get an efficiency grade only.

The score is a prompt for a conversation, not a judgement. Research and planning sessions produce no commits and can still be worth every token. Record that value:

```bash
tokenaudit outcome add resume-engine "closed enterprise pilot" --value 5000
```

## Share it

```bash
tokenaudit review --public --out ./docs        # hashes project names, strips paths and session IDs
tokenaudit review --public --keep-names        # keep project names
```

The Markdown renders cleanly in a GitHub README; the HTML is one self-contained file with light and dark themes.

## How it compares

| Tool | Data source | Strength | Gap tokenaudit fills |
|---|---|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | Claude Code / Codex JSONL | Fast, offline daily and session reports, billing-window tracking | Counts only; no outcome join, no waste diagnosis, no SDK apps |
| [tokscale](https://github.com/junhoyeo/tokscale) | Local logs from 40+ agents | Widest agent coverage, per-workspace rollups, TUI, leaderboard | Counts only. tokenaudit builds on it for non-Claude agents |
| TokenTracker | Hooks into ~30 coding tools | Local dashboard and desktop app | Counts only; no "was it worth it" |
| Claude Code Usage Monitor | Claude Code logs | Burn rate against plan limits | Single tool, real-time only |
| [Langfuse](https://github.com/langfuse/langfuse) | SDK tracing | Full traces, evals, self-hostable | Needs Postgres, ClickHouse, Redis, S3; does not see your CLI agents |
| [Helicone](https://github.com/Helicone/helicone) / [Portkey](https://github.com/Portkey-AI/gateway) / [LiteLLM](https://github.com/BerriAI/litellm) | Proxy or gateway | Budgets, routing, caching in the request path | Sits in your critical path; does not see your CLI agents |
| [llm](https://github.com/simonw/llm) | Its own CLI calls | SQLite logs you own | Only calls made through `llm` |

tokenaudit is the one place where your coding agents and your production apps land in the same ledger, joined to what you shipped.

## Credits

- Non-Claude agent parsing: [tokscale](https://github.com/junhoyeo/tokscale) (MIT).
- Prices: [LiteLLM's model price list](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json), cached weekly, with a bundled snapshot for offline use.
- Inspired by [ccusage](https://github.com/ryoppippi/ccusage) (MIT).

## Roadmap

- Native Codex and Gemini CLI parsers with per-turn detail (or an upstream `tokscale export --jsonl` PR)
- PR-level outcomes: cost per merged PR via `gh`
- Node/TypeScript SDK capture
- GitHub Action that posts a weekly audit to a repo
- Budget alerts per project

## License

MIT
