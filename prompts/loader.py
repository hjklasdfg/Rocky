"""Versioned prompt loader.

Prompts live in prompts/v{N}/{name}.md. Loader reads the file, substitutes
the {context} placeholder, and caches the template.

Why versioned: easy A/B between prompt iterations without git-rewinding.
Set PROMPT_VERSION=v2 in env to swap the whole prompt set at runtime.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
DEFAULT_VERSION = os.getenv("PROMPT_VERSION", "v1")


@lru_cache(maxsize=32)
def _load_template(name: str, version: str) -> str:
    path = PROMPT_DIR / version / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text()


def render(name: str, context: str = "", version: str | None = None) -> str:
    """Load a prompt by name and inject the context block.

    Args:
        name: Prompt file basename (e.g. "router", "email").
        context: Text to substitute for the `{context}` placeholder.
        version: Override the prompt version (defaults to PROMPT_VERSION env or v1).
    """
    template = _load_template(name, version or DEFAULT_VERSION)
    return template.replace("{context}", context)
