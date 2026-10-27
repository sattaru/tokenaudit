
# Token audit

You help the user see where their tokens went, whether the spend bought anything, and what to change. The `tokenaudit` CLI does the counting. Your job is the conversation: read the numbers, explain them plainly, and agree on fixes.

## 1. Make sure the tool is there

```bash
tokenaudit --help >/dev/null 2>&1 || pip install git+https://github.com/sattaru/tokenaudit
```

If the user builds Python apps on the OpenAI, Anthropic or Gemini SDKs, offer to turn on capture for them:

```bash
tokenaudit enable        # one-time; patches the SDKs at import, no code changes
```

## 2. Run the review

```bash
tokenaudit review --out ./token-audit            # all time
tokenaudit review --days 30 --out ./token-audit  # last month
```

This indexes `~/.claude/projects` (Claude Code), pulls Codex/Gemini CLI/Cursor and other agents through tokscale when Node or Bun is installed, joins each project to its git commits, and writes `token-audit.md`, `.html` and `.json`.

Read `token-audit/token-audit.json`. Do not paste the whole report back.

## 3. Give the holistic review

Lead with the answer in 3 to 5 sentences: total spend, the one or two projects that dominate, and the single change worth the most money. Then cover, briefly:

1. **Worth it or not, per project.** Use `projects[].verdict`, `cost_per_commit` and `score`. A project with spend and zero commits is not automatically waste (research, planning, reviews). Ask the user what it produced before calling it waste.
2. **The top 3 findings** from `findings[]`, each with its savings estimate and the concrete fix. Savings overlap, so present the total as an upper bound.
3. **Where context goes.** `tools[]` shows which tools pushed the most text into context and what re-reading it cost. This is usually the biggest lever in Claude Code.
4. **Subscription caveat.** On Claude Max or ChatGPT Pro, dollar figures are value consumed, not cash spent. Translate into "how fast you burn your limits" instead.

## 4. Work with the user on fixes

Ask which finding they want to tackle first, then help do it:

- **cache-miss / cache-churn**: open the app's prompt-building code. Move stable text (system prompt, tool definitions, reference docs) to the front, remove timestamps or random IDs from it, and add `cache_control` breakpoints for Anthropic.
- **context-bloat**: propose a session habit (one task per session, `/clear` between tasks, `/compact` at milestones) and trim `CLAUDE.md`.
- **tool-carry**: for `Read`, suggest grep-then-read-ranges; for `Bash`, quiet flags and `| tail`; for MCP tools, fewer fields or a summarising subagent. Offer to write a project rule into `CLAUDE.md` / `AGENTS.md`.
- **model-overkill**: add a router or switch the default model for short tasks.
- **no-outcome**: ask what the sessions produced. If they had value, record it so future reviews credit it:

  ```bash
  tokenaudit outcome add <project> "shipped onboarding redesign" --value 2000
  ```

Re-run `tokenaudit review --days 7` after a week to show the change.

## 5. Sharing publicly

`tokenaudit review --public` hashes project names and strips paths and session IDs. Add `--keep-names` only if the user says the project names are fine to publish. Check the Markdown before it goes into a public repo.

## Notes

- Prices come from LiteLLM's public price list, refreshed weekly. `tokenaudit prices <model>` shows the rate used.
- Codex and other non-Claude agents arrive as all-time totals per workspace and model, so they count in totals but not in day-by-day views.
- Nothing leaves the machine unless the user publishes the report.
