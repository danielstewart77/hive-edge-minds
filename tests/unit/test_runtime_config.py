"""runtime.yaml is the mind's durable configuration, read and written here."""

import pytest

import runtime_config


RUNTIME = """\
name: atlas
mind_id: 14cb820b-4a42-4f04-a593-54f532fd1d2f
description: "Atlas — test mind."
profile: standard
role: operator
deployment: systemd
harness: claude_cli
provider: anthropic
# The model every new session starts on.
default_model: sonnet
mind_server_port: 8421
gateway_url: http://192.168.4.64:8421
surfaces:
  - telegram
soul_file: souls/atlas.md
"""


@pytest.fixture()
def mind(tmp_path, monkeypatch):
    mind_dir = tmp_path / "minds" / "atlas"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(RUNTIME)
    monkeypatch.setattr(runtime_config, "PROJECT_DIR", tmp_path)
    return mind_dir / "runtime.yaml"


class TestLoad:
    def test_reads_the_file(self, mind):
        assert runtime_config.load_runtime("atlas")["default_model"] == "sonnet"

    def test_public_view_drops_unlisted_fields(self, mind):
        mind.write_text(RUNTIME + "internal_note: not for the console\n")
        assert "internal_note" not in runtime_config.public_runtime("atlas")
        assert runtime_config.public_runtime("atlas")["harness"] == "claude_cli"

    def test_rejects_a_name_that_is_a_path(self):
        with pytest.raises(ValueError):
            runtime_config.runtime_path("../../etc")

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runtime_config, "PROJECT_DIR", tmp_path)
        with pytest.raises(ValueError):
            runtime_config.load_runtime("atlas")


class TestUpdateRuntimeFields:
    def test_writes_the_new_model(self, mind):
        configuration = runtime_config.update_runtime_fields("atlas", {"default_model": "opus"})
        assert configuration["default_model"] == "opus"
        assert runtime_config.load_runtime("atlas")["default_model"] == "opus"

    def test_preserves_comments_and_other_fields(self, mind):
        runtime_config.update_runtime_fields("atlas", {"default_model": "opus"})
        text = mind.read_text()
        assert "# The model every new session starts on." in text
        assert "soul_file: souls/atlas.md" in text
        assert "mind_id: 14cb820b-4a42-4f04-a593-54f532fd1d2f" in text

    def test_accepts_an_ollama_tag(self, mind):
        model = "qwen3:30b-a3b-instruct-2507-q4_K_M"
        assert runtime_config.update_runtime_fields("atlas", {"default_model": model})["default_model"] == model

    @pytest.mark.parametrize("model", ["", "opus; rm -rf /", "opus\nname: evil"])
    def test_rejects_an_unusable_model_name(self, mind, model):
        with pytest.raises(ValueError):
            runtime_config.update_runtime_fields("atlas", {"default_model": model})
        assert runtime_config.load_runtime("atlas")["default_model"] == "sonnet"

    def test_file_without_the_field_is_left_alone(self, mind):
        mind.write_text("name: atlas\nharness: claude_cli\n")
        with pytest.raises(ValueError):
            runtime_config.update_runtime_fields("atlas", {"default_model": "opus"})
        assert "default_model" not in mind.read_text()

    def test_writes_the_provider_alongside_the_model(self, mind):
        """One write, so a mind never holds a provider that lacks its model."""
        saved = runtime_config.update_runtime_fields(
            "atlas", {"provider": "ollama", "default_model": "qwen35-131k"}
        )
        assert (saved["provider"], saved["default_model"]) == ("ollama", "qwen35-131k")

    def test_refuses_a_field_outside_the_writable_set(self, mind):
        with pytest.raises(ValueError):
            runtime_config.update_runtime_fields("atlas", {"gateway_url": "http://x"})

    def test_leaves_no_temporary_files_behind(self, mind):
        runtime_config.update_runtime_fields("atlas", {"default_model": "opus"})
        assert [p.name for p in mind.parent.iterdir()] == ["runtime.yaml"]


class TestRegistrationPayload:
    def test_describes_the_broker_row(self, mind):
        assert runtime_config.registration_payload("atlas") == {
            "mind_id": "14cb820b-4a42-4f04-a593-54f532fd1d2f",
            "name": "atlas",
            "gateway_url": "http://192.168.4.64:8421",
            "model": "sonnet",
            "harness": "claude_cli",
        }

    def test_tracks_an_edit(self, mind):
        runtime_config.update_runtime_fields("atlas", {"default_model": "opus"})
        assert runtime_config.registration_payload("atlas")["model"] == "opus"

    def test_incomplete_file_raises_rather_than_registering_a_half_mind(self, mind):
        mind.write_text("name: atlas\ndefault_model: opus\n")
        with pytest.raises(ValueError) as exc:
            runtime_config.registration_payload("atlas")
        assert "mind_id" in str(exc.value)


class TestAdminToken:
    def test_prefers_the_dedicated_token(self, monkeypatch):
        monkeypatch.setenv("MIND_ADMIN_TOKEN", "mind-token")
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "gateway-token")
        assert runtime_config.admin_token() == "mind-token"

    def test_falls_back_to_the_gateway_bearer(self, monkeypatch):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "gateway-token")
        assert runtime_config.admin_token() == "gateway-token"

    def test_no_token_configured_is_empty_not_open(self, monkeypatch):
        monkeypatch.delenv("MIND_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("COMMS_ADMIN_BEARER_TOKEN", raising=False)
        assert runtime_config.admin_token() == ""
