"""Zero-config usage capture for the OpenAI, Anthropic and Google Gen AI Python SDKs.

    import tokenaudit; tokenaudit.install()          # patch every SDK that is installed
    with tokenaudit.tag("resume-rewrite"): ...        # optional: label a feature/endpoint

Or with no code change at all:  TOKENAUDIT=1 python app.py   (via sitecustomize, see README)

Tracking never raises into your app. If logging fails, the call still returns normally.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import pricing, store

_tag = contextvars.ContextVar("tokenaudit_tag", default="")
_project_override = contextvars.ContextVar("tokenaudit_project", default="")
_installed: set[str] = set()


@contextlib.contextmanager
def tag(label: str, project: str | None = None):
    """Attribute every call inside this block to a feature label (and optionally a project)."""
    t1 = _tag.set(label)
    t2 = _project_override.set(project or _project_override.get())
    try:
        yield
    finally:
        _tag.reset(t1)
        _project_override.reset(t2)


@functools.lru_cache(maxsize=64)
def _project_for(cwd: str) -> tuple[str, str]:
    try:
        top = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if top:
            return Path(top).name, top
    except Exception:
        pass
    return Path(cwd).name, cwd


def _project() -> tuple[str, str]:
    p = _project_override.get() or os.environ.get("TOKENAUDIT_PROJECT", "")
    name, ws = _project_for(os.getcwd())
    return (p or name), ws


# ---------- usage extraction (works on SDK objects and plain dicts) ----------

def _g(obj, *path, default=0):
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def usage_openai(u) -> dict:
    if u is None:
        return {}
    if _g(u, "prompt_tokens", default=None) is not None:            # chat.completions
        prompt, cached = _g(u, "prompt_tokens"), _g(u, "prompt_tokens_details", "cached_tokens")
        return {"input": prompt - cached, "cache_read": cached,
                "output": _g(u, "completion_tokens"),
                "reasoning": _g(u, "completion_tokens_details", "reasoning_tokens")}
    inp, cached = _g(u, "input_tokens"), _g(u, "input_tokens_details", "cached_tokens")  # responses
    return {"input": inp - cached, "cache_read": cached, "output": _g(u, "output_tokens"),
            "reasoning": _g(u, "output_tokens_details", "reasoning_tokens")}


def usage_anthropic(u) -> dict:
    if u is None:
        return {}
    return {"input": _g(u, "input_tokens"), "output": _g(u, "output_tokens"),
            "cache_read": _g(u, "cache_read_input_tokens"),
            "cache_write": _g(u, "cache_creation_input_tokens")}


def usage_gemini(u) -> dict:
    if u is None:
        return {}
    prompt, cached = _g(u, "prompt_token_count"), _g(u, "cached_content_token_count")
    thoughts = _g(u, "thoughts_token_count")
    return {"input": prompt - cached + _g(u, "tool_use_prompt_token_count"), "cache_read": cached,
            "output": _g(u, "candidates_token_count") + thoughts, "reasoning": thoughts}


def _record(provider: str, model: str, usage: dict, started: float, client: str = "") -> None:
    try:
        if not usage or not any(usage.values()):
            return
        name, ws = _project()
        u = {k: int(usage.get(k, 0) or 0) for k in ("input", "output", "cache_read", "cache_write", "reasoning")}
        c = pricing.cost(model, u["input"], u["output"], u["cache_read"], u["cache_write"])
        store.upsert([store.Event(
            dedup_key=f"sdk:{uuid.uuid4().hex}",
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source="sdk", client=client or os.environ.get("TOKENAUDIT_APP", name),
            provider=provider, model=model or "", project=name, workspace=ws,
            session=os.environ.get("TOKENAUDIT_SESSION", f"pid-{os.getpid()}"),
            cost=c, latency_ms=int((time.time() - started) * 1000), tag=_tag.get(), **u)])
    except Exception as e:  # never break the caller
        if os.environ.get("TOKENAUDIT_DEBUG"):
            print(f"[tokenaudit] record failed: {e!r}")


# ---------- stream wrappers ----------

class _StreamProxy:
    """Wraps a sync or async SDK stream, harvests usage as chunks pass through."""

    def __init__(self, inner, provider, model, started, harvest):
        self._inner, self._provider, self._model = inner, provider, model
        self._started, self._harvest, self._usage, self._done = started, harvest, {}, False

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def _see(self, chunk):
        try:
            got, model = self._harvest(chunk)
            if model:
                self._model = model
            for k, v in (got or {}).items():
                if v:
                    self._usage[k] = max(self._usage.get(k, 0), v)  # all providers report cumulative counts
        except Exception:
            pass

    def _finish(self):
        if not self._done:
            self._done = True
            _record(self._provider, self._model, self._usage, self._started)

    def __iter__(self):
        try:
            for ch in self._inner:
                self._see(ch)
                yield ch
        finally:
            self._finish()

    async def __aiter__(self):
        try:
            async for ch in self._inner:
                self._see(ch)
                yield ch
        finally:
            self._finish()

    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, *a):
        self._finish()
        return self._inner.__exit__(*a) if hasattr(self._inner, "__exit__") else False

    async def __aenter__(self):
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, *a):
        self._finish()
        return await self._inner.__aexit__(*a) if hasattr(self._inner, "__aexit__") else False


def _harvest_openai(ch):
    u = _g(ch, "usage", default=None) or _g(ch, "response", "usage", default=None)
    return (usage_openai(u) if u else {}), _g(ch, "model", default="") or _g(ch, "response", "model", default="")


def _harvest_anthropic(ch):
    t = _g(ch, "type", default="")
    if t == "message_start":
        m = _g(ch, "message", default=None)
        return usage_anthropic(_g(m, "usage", default=None)), _g(m, "model", default="")
    if t == "message_delta":
        u = _g(ch, "usage", default=None)
        return {"output": _g(u, "output_tokens")}, ""
    return {}, ""


def _harvest_gemini(ch):
    u = _g(ch, "usage_metadata", default=None)
    return (usage_gemini(u) if u else {}), _g(ch, "model_version", default="")


# ---------- patching ----------

def _wrap(fn, provider, extract, harvest, is_async, model_from, stream_check):
    if getattr(fn, "_tokenaudit", False):
        return fn

    def prep(kwargs):
        if provider == "openai-chat" and kwargs.get("stream") and "stream_options" not in kwargs:
            kwargs["stream_options"] = {"include_usage": True}   # otherwise OpenAI sends no usage
        return kwargs

    prov = provider.split("-")[0]
    if prov == "gemini":
        prov = "google"

    if is_async:
        @functools.wraps(fn)
        async def aw(*a, **kw):
            started = time.time()
            resp = await fn(*a, **prep(kw))
            if stream_check(kw, resp):
                return _StreamProxy(resp, prov, model_from(a, kw, None), started, harvest)
            _record(prov, model_from(a, kw, resp), extract(resp), started)
            return resp
        aw._tokenaudit = True
        return aw

    @functools.wraps(fn)
    def w(*a, **kw):
        started = time.time()
        resp = fn(*a, **prep(kw))
        if stream_check(kw, resp):
            return _StreamProxy(resp, prov, model_from(a, kw, None), started, harvest)
        _record(prov, model_from(a, kw, resp), extract(resp), started)
        return resp
    w._tokenaudit = True
    return w


def _model_kw(a, kw, resp):
    return _g(resp, "model", default="") or kw.get("model", "")


def _model_gemini(a, kw, resp):
    return _g(resp, "model_version", default="") or kw.get("model", "")


def _patch(cls, name, *args):
    orig = getattr(cls, name, None)
    if orig is not None:
        setattr(cls, name, _wrap(orig, *args))


def install(providers=("openai", "anthropic", "gemini")) -> list[str]:
    """Patch installed SDKs in place. Safe to call more than once."""
    done = []
    is_stream = lambda kw, resp: bool(kw.get("stream"))
    if "openai" in providers and "openai" not in _installed:
        try:
            from openai.resources.chat import completions as cc
            from openai.resources import responses as rr
            ex = lambda r: usage_openai(_g(r, "usage", default=None))
            for cls, a in ((cc.Completions, False), (cc.AsyncCompletions, True)):
                _patch(cls, "create", "openai-chat", ex, _harvest_openai, a, _model_kw, is_stream)
            for cls, a in ((rr.Responses, False), (rr.AsyncResponses, True)):
                _patch(cls, "create", "openai-resp", ex, _harvest_openai, a, _model_kw, is_stream)
            _installed.add("openai"); done.append("openai")
        except ImportError:
            pass
    if "anthropic" in providers and "anthropic" not in _installed:
        try:
            from anthropic.resources import messages as mm
            ex = lambda r: usage_anthropic(_g(r, "usage", default=None))
            for cls, a in ((mm.Messages, False), (mm.AsyncMessages, True)):
                _patch(cls, "create", "anthropic", ex, _harvest_anthropic, a, _model_kw, is_stream)
            _installed.add("anthropic"); done.append("anthropic")
        except ImportError:
            pass
    if "gemini" in providers and "gemini" not in _installed:
        try:
            from google.genai import models as gm
            ex = lambda r: usage_gemini(_g(r, "usage_metadata", default=None))
            for cls, a in ((gm.Models, False), (gm.AsyncModels, True)):
                _patch(cls, "generate_content", "gemini", ex, _harvest_gemini, a, _model_gemini, lambda kw, r: False)
                _patch(cls, "generate_content_stream", "gemini", ex, _harvest_gemini, a, _model_gemini, lambda kw, r: True)
            _installed.add("gemini"); done.append("gemini")
        except ImportError:
            pass
    return done


def prompt_fingerprint(text: str) -> str:
    """Stable short hash, used to spot repeated prompts without storing them."""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
