# Security

## What tokenaudit stores

Token counts, model names, timestamps, project and workspace names, session IDs, tool names and estimated tool output sizes. It never stores prompts, responses, file contents or API keys. Data lives in `~/.tokenaudit/usage.db` on your machine.

The only network call is a weekly download of LiteLLM's public price list from raw.githubusercontent.com. Set `TOKENAUDIT_HOME` to move the data folder.

## Before you publish a report

Use `tokenaudit review --public`. It hashes project names and strips paths and session IDs. Read the Markdown before you commit it.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting on this repo (Security tab, "Report a vulnerability"). I aim to reply within 7 days.
