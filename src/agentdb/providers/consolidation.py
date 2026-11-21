"""
Consolidation helper — build an ``llm_fn`` for AgentDB consolidation synthesis.

AgentDB's ``consolidate(agent_id, llm_fn=...)`` accepts a plain callable
``llm_fn(prompt: str) -> str`` used to synthesize a canonical memory from a
cluster of near-duplicate records. Without one, consolidation falls back to
``cluster_copy`` (verbatim copy of the top source) instead of
``cluster_synthesis``.

Every integrator ends up writing the same boilerplate: construct an
OpenAI-compatible client, build chat-completion kwargs, call the model, extract
the text. ``build_llm_fn`` does all of that once, so an integrator only supplies
the model id + connection params — no client construction, no wrapping function.

Example::

    from agentdb.providers.consolidation import build_llm_fn

    llm_fn = build_llm_fn(
        model="meta-llama/Meta-Llama-3.1-8B-Instruct",
        base_url="https://api.deepinfra.com/v1/openai",
        api_key="...",
    )
    db.consolidate(agent_id="my-agent", llm_fn=llm_fn)

The result is synchronous (consolidate calls ``llm_fn`` synchronously) and
speaks the OpenAI Chat Completions API via the ``openai`` package. That package
is optional: install it with ``pip install 'agentdatabase[llm]'`` (or have the
``providers``/``local-embed`` extras that already pull a chat client). If it is
not installed, ``build_llm_fn`` raises ImportError with a clear remedy.

Errors from the model call are NOT swallowed here — ``consolidate`` already
wraps ``llm_fn`` in try/except and falls back to the source value on failure, so
a raised error is handled upstream and never blocks consolidation.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

_DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"


def build_llm_fn(
    model: str,
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: Optional[float] = None,
) -> Callable[[str], str]:
    """Build an ``llm_fn(prompt) -> str`` for ``AgentDB.consolidate(...)``.

    Constructs a synchronous OpenAI-compatible client from the supplied
    connection params and returns a callable that sends a single user message
    and returns the stripped assistant text.

    Args:
        model: Model id to use for synthesis (e.g. a small/cheap model).
        base_url: OpenAI-compatible endpoint. Defaults to DeepInfra's.
        api_key: API key for the endpoint (use a placeholder for local servers).
        max_tokens: Optional max tokens for the response.
        temperature: Optional sampling temperature.
        timeout: Optional request timeout in seconds.

    Returns:
        ``llm_fn(prompt: str) -> str`` suitable for ``consolidate(llm_fn=...)``.

    Raises:
        ValueError: if ``model`` is empty.
        ImportError: if the ``openai`` package is not installed.
    """
    if not model or not str(model).strip():
        raise ValueError("build_llm_fn requires a non-empty model id")

    try:
        from openai import OpenAI  # sync client matches consolidate's call site
    except ImportError as e:  # pragma: no cover - exercised via monkeypatch in tests
        raise ImportError(
            "build_llm_fn needs the 'openai' package for chat completions. "
            "Install it with: pip install 'openai' "
            "(or use agentdatabase extras that include a chat client)."
        ) from e

    client_kwargs: dict[str, Any] = {
        "api_key": api_key or "not-needed",
        "base_url": base_url or _DEFAULT_BASE_URL,
    }
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    client = OpenAI(**client_kwargs)

    def _llm_fn(prompt: str) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    return _llm_fn


__all__ = ["build_llm_fn"]
