"""Provider routing across the two harness templates.

Provider is orthogonal to harness: either CLI runs against Ollama, and both
keep the browser terminal when it does. The claude harness gets there through
``provider.env_overrides`` alone; codex additionally needs ``-c
model_providers.*`` flags, because it has no base-URL environment variable.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import mind_templates.claude_cli as claude_impl  # noqa: E402
import mind_templates.codex_cli as codex_impl  # noqa: E402
from models import ModelRegistry, Provider  # noqa: E402

# Every function the browser terminal and the rotation path call on an
# implementation module. The deleted *_ollama templates had none of them, so
# an Ollama mind silently lost its terminal.
_TERMINAL_API = (
    "spawn_pty",
    "rotate_pty_session",
    "pty_session_alive",
    "kill_pty_session",
    "tmux_session_name",
)


def _registry(env=None, api_base=None):
    return ModelRegistry(
        providers={
            "anthropic": Provider(name="anthropic"),
            "openai": Provider(name="openai"),
            "ollama": Provider(
                name="ollama", env_overrides=env or {}, api_base=api_base
            ),
        },
        static_models={"sonnet": "anthropic", "gpt-5.4": "openai"},
    )


@pytest.mark.parametrize("impl", [claude_impl, codex_impl], ids=["claude", "codex"])
def test_both_harnesses_keep_the_browser_terminal(impl):
    for name in _TERMINAL_API:
        assert callable(getattr(impl, name, None)), f"{impl.__name__} lost {name}"


def test_codex_emits_provider_args_for_ollama():
    provider = _registry({"OLLAMA_BASE_URL": "http://proxy:8899/v1"}).get_provider(
        "qwen3:8b"
    )
    args = codex_impl._provider_args(provider, "atlas")

    joined = " ".join(args)
    assert 'model_provider="atlas_ollama"' in joined
    assert 'model_providers.atlas_ollama.base_url="http://proxy:8899/v1"' in joined
    # No key configured — codex must not declare env_key, or bare Ollama gets
    # an Authorization header it never asked for.
    assert "env_key" not in joined


def test_codex_declares_env_key_only_when_a_key_exists():
    provider = _registry(
        {"OLLAMA_BASE_URL": "http://proxy:8899/v1", "OPENAI_API_KEY": "hmp-abc"}
    ).get_provider("qwen3:8b")

    with patch.dict("os.environ", {}, clear=False):
        args = codex_impl._provider_args(provider, "atlas")

    assert 'model_providers.atlas_ollama.env_key="OPENAI_API_KEY"' in " ".join(args)


def test_codex_falls_back_to_api_base_with_the_openai_path():
    """api_base is the anthropic-shaped field; codex speaks the
    OpenAI-compatible dialect, which Ollama serves under /v1."""
    provider = _registry(api_base="http://ollama-host:11434").get_provider("qwen3:8b")
    args = codex_impl._provider_args(provider, "atlas")

    assert 'base_url="http://ollama-host:11434/v1"' in " ".join(args)


@pytest.mark.parametrize("model", ["sonnet", "gpt-5.4"])
def test_codex_emits_nothing_for_first_party_providers(model):
    """A non-Ollama mind must spawn the exact argv it did before."""
    provider = _registry().get_provider(model)
    assert codex_impl._provider_args(provider, "atlas") == []
    assert codex_impl._base_cmd([]) == codex_impl._base_cmd()


def test_codex_provider_args_land_in_the_spawned_command():
    provider = _registry({"OLLAMA_BASE_URL": "http://proxy:8899/v1"}).get_provider(
        "qwen3:8b"
    )
    cmd = codex_impl._base_cmd(codex_impl._provider_args(provider, "atlas"))

    assert cmd[:3] == ["codex", "exec", "--json"]
    # `-c` flags are global options: they must precede the subcommand
    # positional that send() appends ("resume <id>" / "-").
    assert 'model_provider="atlas_ollama"' in cmd


def test_codex_terminal_argv_carries_provider_args():
    """The browser terminal has to reach the same provider the chat turns do."""
    args = codex_impl._provider_args(
        _registry({"OLLAMA_BASE_URL": "http://proxy:8899/v1"}).get_provider("qwen3:8b"),
        "atlas",
    )
    argv = codex_impl._terminal_argv(None, args)

    assert argv[0] == "codex"
    assert 'model_provider="atlas_ollama"' in argv


def test_unresolvable_model_does_not_kill_the_turn():
    registry = ModelRegistry(providers={"anthropic": Provider(name="anthropic")},
                             static_models={"sonnet": "anthropic"})

    assert codex_impl._resolve_provider(registry, "no-such-model") is None
    assert codex_impl._resolve_provider(None, "sonnet") is None
    assert codex_impl._provider_args(None, "atlas") == []
