"""Regression tests for the scaffolded Ollama harness templates."""

import pytest

import mind_templates.codex_cli_ollama as codex_ollama


@pytest.fixture(autouse=True)
def _clear_sessions():
    codex_ollama._sessions.clear()
    yield
    codex_ollama._sessions.clear()


@pytest.mark.asyncio
async def test_codex_ollama_spawn_combines_bootstrap_and_surface_prompts():
    state = await codex_ollama.spawn(
        "session-1",
        "ollama/model",
        system_prompt_blocks="trusted bootstrap",
        surface_prompt="telegram instructions",
    )

    assert state["system_prompt"] == "trusted bootstrap\n\ntelegram instructions"
    assert codex_ollama._sessions["session-1"] is state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bootstrap", "surface", "expected"),
    [
        ("trusted bootstrap", None, "trusted bootstrap"),
        ("", "terminal instructions", "terminal instructions"),
        ("", None, ""),
    ],
)
async def test_codex_ollama_spawn_handles_optional_prompt_parts(
    bootstrap, surface, expected
):
    state = await codex_ollama.spawn(
        "session-1",
        "ollama/model",
        system_prompt_blocks=bootstrap,
        surface_prompt=surface,
    )

    assert state["system_prompt"] == expected
