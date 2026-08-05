"""Tests for the Anthropic /v1/messages model allow/deny list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from headroom.proxy.model_allowlist import ModelAllowlist, ModelAllowlistConfig
from headroom.proxy.server import ProxyConfig, _proxy_config_from_env, create_app

MESSAGES = "/v1/messages"

# ---------------------------------------------------------------------------
# ModelAllowlist.check
# ---------------------------------------------------------------------------


def test_check_passes_everything_when_disabled() -> None:
    allowlist = ModelAllowlist(ModelAllowlistConfig())
    assert allowlist.check("gpt-4o") is None


def test_check_allows_matching_glob() -> None:
    allowlist = ModelAllowlist(ModelAllowlistConfig(enabled=True, allow=("claude-*",)))
    assert allowlist.check("claude-sonnet-4-6") is None


def test_check_blocks_non_matching_model() -> None:
    allowlist = ModelAllowlist(ModelAllowlistConfig(enabled=True, allow=("claude-*",)))
    reason = allowlist.check("gpt-4o")
    assert reason is not None
    assert "gpt-4o" in reason


def test_check_deny_wins_over_allow() -> None:
    allowlist = ModelAllowlist(
        ModelAllowlistConfig(enabled=True, allow=("claude-*",), deny=("claude-haiku-*",))
    )
    assert allowlist.check("claude-sonnet-4-6") is None
    reason = allowlist.check("claude-haiku-4-5")
    assert reason is not None and "contact your administrator" in reason


def test_check_deny_only_allows_everything_else() -> None:
    allowlist = ModelAllowlist(ModelAllowlistConfig(enabled=True, deny=("claude-haiku-*",)))
    assert allowlist.check("gpt-4o") is None
    assert allowlist.check("claude-haiku-4-5") is not None


def test_check_is_case_insensitive() -> None:
    allowlist = ModelAllowlist(ModelAllowlistConfig(enabled=True, allow=("claude-*",)))
    assert allowlist.check("CLAUDE-Sonnet-4-6") is None


# ---------------------------------------------------------------------------
# ModelAllowlistConfig.from_env (fail-open parsing)
# ---------------------------------------------------------------------------


def test_from_env_disabled_by_default() -> None:
    cfg = ModelAllowlistConfig.from_env(None, None, None)
    assert not cfg.enabled
    assert cfg.allow == ()
    assert cfg.deny == ()


def test_from_env_parses_allow_and_deny() -> None:
    cfg = ModelAllowlistConfig.from_env("true", '["claude-*"]', '["claude-haiku-*"]')
    assert cfg.enabled
    assert cfg.allow == ("claude-*",)
    assert cfg.deny == ("claude-haiku-*",)


def test_from_env_enabled_without_patterns_disables(caplog) -> None:
    cfg = ModelAllowlistConfig.from_env("true", None, None)
    assert not cfg.enabled


def test_from_env_malformed_json_ignored() -> None:
    cfg = ModelAllowlistConfig.from_env("true", "not json", '["claude-haiku-*"]')
    assert cfg.allow == ()
    assert cfg.deny == ("claude-haiku-*",)
    assert cfg.enabled


def test_from_env_non_string_list_entries_ignored() -> None:
    cfg = ModelAllowlistConfig.from_env("true", "[1, 2]", None)
    assert cfg.allow == ()


def test_from_env_reads_patterns_from_file(tmp_path) -> None:
    path = tmp_path / "allow.json"
    path.write_text('["claude-*"]', encoding="utf-8")
    cfg = ModelAllowlistConfig.from_env("true", str(path), None)
    assert cfg.allow == ("claude-*",)


# ---------------------------------------------------------------------------
# Wiring: env -> ProxyConfig -> live proxy
# ---------------------------------------------------------------------------


def test_proxy_config_from_env_reads_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("HEADROOM_MODEL_ALLOWLIST_ENABLED", "true")
    monkeypatch.setenv("HEADROOM_MODEL_ALLOWLIST_ALLOW", '["claude-*"]')
    monkeypatch.delenv("HEADROOM_MODEL_ALLOWLIST_DENY", raising=False)
    config = _proxy_config_from_env()
    assert config.model_allowlist is not None
    assert config.model_allowlist.enabled
    assert config.model_allowlist.allow == ("claude-*",)


def test_proxy_config_from_env_allowlist_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HEADROOM_MODEL_ALLOWLIST_ENABLED", raising=False)
    monkeypatch.delenv("HEADROOM_MODEL_ALLOWLIST_ALLOW", raising=False)
    monkeypatch.delenv("HEADROOM_MODEL_ALLOWLIST_DENY", raising=False)
    config = _proxy_config_from_env()
    assert config.model_allowlist is not None
    assert not config.model_allowlist.enabled


def _allowlist_config() -> ProxyConfig:
    return ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode="token",
        model_allowlist=ModelAllowlistConfig(enabled=True, allow=("claude-*",)),
    )


def test_disallowed_model_is_rejected_with_400() -> None:
    app = create_app(_allowlist_config())
    with TestClient(app) as client:
        resp = client.post(
            MESSAGES,
            json={
                "model": "gpt-4o",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "contact your administrator" in body["error"]["message"]


def test_allowed_model_passes_through(monkeypatch) -> None:
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    app = create_app(_allowlist_config())
    with TestClient(app) as client:
        proxy = client.app.state.proxy
        response = httpx.Response(
            200, json={"ok": True}, request=httpx.Request("POST", "http://upstream/v1/messages")
        )
        http = MagicMock()
        http.post = AsyncMock(return_value=response)
        http.aclose = AsyncMock()
        proxy.http_client = http

        resp = client.post(
            MESSAGES,
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200


def test_allowlist_disabled_lets_any_model_through() -> None:
    app = create_app(ProxyConfig(optimize=False, cost_tracking_enabled=False))
    with TestClient(app) as client:
        assert not client.app.state.proxy.model_allowlist.enabled
