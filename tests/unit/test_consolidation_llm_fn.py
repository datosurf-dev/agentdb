"""Tests for agentdb.providers.consolidation.build_llm_fn."""

import sys
import types

import pytest

from agentdb.providers import consolidation


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.append(kwargs)
        return _Resp("  synthesized text  ")


class _FakeOpenAI:
    instances = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []
        self.chat = types.SimpleNamespace(completions=_Completions(self.calls))
        _FakeOpenAI.instances.append(self)


@pytest.fixture(autouse=True)
def _fake_openai(monkeypatch):
    """Make ``from openai import OpenAI`` resolve to our fake."""
    fake_mod = types.ModuleType("openai")
    fake_mod.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_mod)
    _FakeOpenAI.instances = []
    yield


def test_builds_client_and_strips_content():
    fn = consolidation.build_llm_fn(
        "small-model", base_url="https://example/v1", api_key="k"
    )
    assert callable(fn)
    assert fn("hello") == "synthesized text"
    client = _FakeOpenAI.instances[-1]
    assert client.init_kwargs["base_url"] == "https://example/v1"
    assert client.init_kwargs["api_key"] == "k"
    call = client.calls[0]
    assert call["model"] == "small-model"
    assert call["messages"] == [{"role": "user", "content": "hello"}]
    assert "max_tokens" not in call and "temperature" not in call


def test_defaults_base_url_and_api_key_placeholder():
    consolidation.build_llm_fn("m")
    client = _FakeOpenAI.instances[-1]
    assert client.init_kwargs["base_url"] == consolidation._DEFAULT_BASE_URL
    assert client.init_kwargs["api_key"] == "not-needed"


def test_forwards_optional_params():
    fn = consolidation.build_llm_fn("m", max_tokens=256, temperature=0.2)
    fn("x")
    call = _FakeOpenAI.instances[-1].calls[0]
    assert call["max_tokens"] == 256
    assert call["temperature"] == pytest.approx(0.2)


def test_empty_model_rejected():
    with pytest.raises(ValueError):
        consolidation.build_llm_fn("")


def test_missing_openai_raises_importerror(monkeypatch):
    # Simulate openai not installed.
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(ImportError):
        consolidation.build_llm_fn("m")
