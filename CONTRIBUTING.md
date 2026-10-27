# Contributing

Thanks for helping. Bug reports, new findings and new agent sources are all welcome.

## Set up

```bash
git clone https://github.com/sattaru/tokenaudit
cd tokenaudit
pip install -e ".[test]"
pytest -q
```

## Good first contributions

- **A new finding.** Add it in `tokenaudit/analyze.py` with a test in `tests/`. Every finding needs evidence, a savings estimate the user can check, and a fix that names a concrete action.
- **A new SDK.** Add a usage extractor and patch target in `tokenaudit/track.py`. Tests must not call a real API: use a mock transport like the existing ones.
- **A native log parser** for Codex CLI or Gemini CLI with per-turn detail.
- **Price fixes.** Prices come from LiteLLM. If a model is wrong or missing, fix it upstream in [BerriAI/litellm](https://github.com/BerriAI/litellm) so everyone benefits.

## Rules

- Never store prompt or response text. Token counts, model names, tool names and sizes only.
- Tracking must never raise into the user's app.
- No new runtime dependencies without a strong reason. The core runs on the standard library.
- Keep the user's data local. Nothing leaves the machine unless the user publishes a report.

## Pull requests

Keep them small and focused, describe the user-visible change, and make sure `pytest -q` passes.
