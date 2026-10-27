"""Import-time auto tracking.

`tokenaudit enable` drops a one-line .pth file into site-packages that imports this
module at Python startup. It costs nothing until your code imports an AI SDK: a light
import hook waits for openai / anthropic / google.genai and patches them on first load.
Set TOKENAUDIT_OFF=1 to disable for a process.
"""
from __future__ import annotations

import importlib.abc
import os
import sys

_TARGETS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google.genai": "gemini",
}


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        prov = _TARGETS.get(name)
        if not prov:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec and spec.loader and hasattr(spec.loader, "exec_module"):
                orig = spec.loader.exec_module

                def exec_module(module, _orig=orig, _prov=prov):
                    _orig(module)
                    _after(_prov)
                spec.loader.exec_module = exec_module
                return spec
        return None


def _after(prov: str) -> None:
    try:
        from .track import install
        install(providers=(prov,))
    except Exception as e:
        if os.environ.get("TOKENAUDIT_DEBUG"):
            print(f"[tokenaudit] auto-install failed for {prov}: {e!r}")


if not os.environ.get("TOKENAUDIT_OFF"):
    already = [p for m, p in _TARGETS.items() if m in sys.modules]
    for p in already:
        _after(p)
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())
