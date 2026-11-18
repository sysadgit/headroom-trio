"""Tests for per-device tokens that unlock models denied by the allowlist."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from headroom.proxy import device_tokens
from headroom.proxy.device_tokens import (
    TOKEN_HEADER,
    DeviceTokenStore,
    add_device,
    hash_token,
    list_devices,
    revoke_device,
)
from headroom.proxy.model_allowlist import ModelAllowlistConfig
from headroom.proxy.server import ProxyConfig, create_app

MESSAGES = "/v1/messages"
FABLE = "claude-fable-5"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def test_missing_file_is_a_noop(tmp_path) -> None:
    store = DeviceTokenStore(tmp_path / "nope.json")
    assert store.lookup("hrt_anything") is None


def test_add_then_lookup_roundtrip(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("anand-macbook", path=path)
    store = DeviceTokenStore(path)
    device = store.lookup(secret)
    assert device is not None and device.name == "anand-macbook"
    assert device.permits("claude-fable-5")
    assert not device.permits("claude-opus-4")


def test_file_never_contains_the_secret(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("d1", path=path)
    text = path.read_text()
    assert secret not in text
    assert hash_token(secret) in text


def test_file_permissions_are_private(tmp_path) -> None:
    path = tmp_path / "t.json"
    add_device("d1", path=path)
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_wrong_empty_and_oversized_tokens_rejected(tmp_path) -> None:
    path = tmp_path / "t.json"
    add_device("d1", path=path)
    store = DeviceTokenStore(path)
    assert store.lookup("hrt_wrong") is None
    assert store.lookup("") is None
    assert store.lookup(None) is None
    assert store.lookup("x" * 5000) is None


def test_duplicate_name_rejected(tmp_path) -> None:
    path = tmp_path / "t.json"
    add_device("d1", path=path)
    with pytest.raises(ValueError):
        add_device("d1", path=path)


def test_revoke_takes_effect_without_restart(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("d1", path=path)
    store = DeviceTokenStore(path)
    assert store.lookup(secret) is not None
    assert revoke_device("d1", path=path) is True
    assert store.lookup(secret) is None
    assert revoke_device("d1", path=path) is False


def test_expired_token_rejected(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("d1", path=path, days=1)
    entries = json.loads(path.read_text())
    entries[0]["expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(entries))
    assert DeviceTokenStore(path).lookup(secret) is None


def test_unparseable_expiry_fails_closed(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("d1", path=path)
    entries = json.loads(path.read_text())
    entries[0]["expires_at"] = "not-a-date"
    path.write_text(json.dumps(entries))
    assert DeviceTokenStore(path).lookup(secret) is None


def test_malformed_reload_keeps_last_good_list(tmp_path) -> None:
    path = tmp_path / "t.json"
    secret = add_device("d1", path=path)
    store = DeviceTokenStore(path)
    assert store.lookup(secret) is not None
    time.sleep(0.01)
    path.write_text("{ this is not json")
    assert store.lookup(secret) is not None  # typo must not revoke anyone


def test_malformed_file_at_startup_grants_nothing(tmp_path) -> None:
    path = tmp_path / "t.json"
    path.write_text("garbage")
    assert DeviceTokenStore(path).lookup("hrt_x") is None


def test_bad_entry_shapes_are_rejected(tmp_path) -> None:
    path = tmp_path / "t.json"
    for bad in (
        [{"name": "", "sha256": "a" * 64, "allow": []}],
        [{"name": "x", "sha256": "short", "allow": []}],
        [{"name": "x", "sha256": "z" * 64, "allow": []}],
        [{"name": "x", "sha256": "a" * 64, "allow": "claude-fable*"}],
        {"name": "x"},
    ):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            list_devices(path=path)


def test_default_path_resolution(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HEADROOM_DEVICE_TOKENS_FILE", raising=False)
    monkeypatch.setenv("HEADROOM_CONFIG_DIR", str(tmp_path / "cfg"))
    assert device_tokens.default_token_file() == tmp_path / "cfg" / "device_tokens.json"
    monkeypatch.setenv("HEADROOM_DEVICE_TOKENS_FILE", str(tmp_path / "x.json"))
    assert device_tokens.default_token_file() == tmp_path / "x.json"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_add_list_revoke(tmp_path) -> None:
    from headroom.cli.main import main

    path = str(tmp_path / "t.json")
    runner = CliRunner()
    added = runner.invoke(main, ["token", "add", "anand-macbook", "--file", path])
    assert added.exit_code == 0, added.output
    assert "hrt_" in added.output
    listed = runner.invoke(main, ["token", "list", "--file", path])
    assert "anand-macbook" in listed.output and "hrt_" not in listed.output
    assert runner.invoke(main, ["token", "revoke", "anand-macbook", "--file", path]).exit_code == 0
    assert runner.invoke(main, ["token", "revoke", "anand-macbook", "--file", path]).exit_code != 0
    assert "No devices" in runner.invoke(main, ["token", "list", "--file", path]).output


# ---------------------------------------------------------------------------
# End to end through the app
# ---------------------------------------------------------------------------


def _config() -> ProxyConfig:
    return ProxyConfig(
        optimize=False,
        cache_enabled=False,
        rate_limit_enabled=False,
        cost_tracking_enabled=False,
        ccr_inject_tool=False,
        ccr_handle_responses=False,
        ccr_context_tracking=False,
        mode="token",
        model_allowlist=ModelAllowlistConfig(enabled=True, deny=("claude-fable*", "fable*")),
    )


def _body(model: str) -> dict:
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


def _client(monkeypatch, tmp_path, secret_file=True):
    path = tmp_path / "t.json"
    monkeypatch.setenv("HEADROOM_DEVICE_TOKENS_FILE", str(path))
    app = create_app(_config())
    client = TestClient(app)
    client.__enter__()
    upstream = MagicMock()
    upstream.post = AsyncMock(
        return_value=httpx.Response(
            200, json={"ok": True}, request=httpx.Request("POST", "http://up/v1/messages")
        )
    )
    upstream.aclose = AsyncMock()
    client.app.state.proxy.http_client = upstream
    return client, upstream, path


def test_fable_blocked_without_token(monkeypatch, tmp_path) -> None:
    client, upstream, _ = _client(monkeypatch, tmp_path)
    try:
        resp = client.post(MESSAGES, json=_body(FABLE))
        assert resp.status_code == 400
        assert "not supported by your organization" in resp.json()["error"]["message"]
        upstream.post.assert_not_called()
    finally:
        client.__exit__(None, None, None)


def test_fable_allowed_with_valid_token_and_header_not_forwarded(monkeypatch, tmp_path) -> None:
    client, upstream, path = _client(monkeypatch, tmp_path)
    try:
        secret = add_device("anand-macbook", path=path)
        resp = client.post(MESSAGES, json=_body(FABLE), headers={TOKEN_HEADER: secret})
        assert resp.status_code == 200
        sent = upstream.post.call_args.kwargs.get("headers") or {}
        assert TOKEN_HEADER not in {k.lower() for k in sent}
        assert secret not in json.dumps(sent)
    finally:
        client.__exit__(None, None, None)


def test_fable_blocked_with_wrong_token(monkeypatch, tmp_path) -> None:
    client, _, path = _client(monkeypatch, tmp_path)
    try:
        add_device("anand-macbook", path=path)
        resp = client.post(MESSAGES, json=_body(FABLE), headers={TOKEN_HEADER: "hrt_nope"})
        assert resp.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_revoked_token_blocked_immediately(monkeypatch, tmp_path) -> None:
    client, _, path = _client(monkeypatch, tmp_path)
    try:
        secret = add_device("anand-macbook", path=path)
        ok = client.post(MESSAGES, json=_body(FABLE), headers={TOKEN_HEADER: secret})
        assert ok.status_code == 200
        revoke_device("anand-macbook", path=path)
        blocked = client.post(MESSAGES, json=_body(FABLE), headers={TOKEN_HEADER: secret})
        assert blocked.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_token_only_unlocks_what_its_entry_allows(monkeypatch, tmp_path) -> None:
    client, _, path = _client(monkeypatch, tmp_path)
    try:
        secret = add_device("narrow", path=path, allow=("claude-fable-5",))
        other = client.post(MESSAGES, json=_body("claude-fable-6"), headers={TOKEN_HEADER: secret})
        assert other.status_code == 400
    finally:
        client.__exit__(None, None, None)


def test_normal_models_unaffected_with_or_without_token(monkeypatch, tmp_path) -> None:
    client, _, path = _client(monkeypatch, tmp_path)
    try:
        secret = add_device("d", path=path)
        assert client.post(MESSAGES, json=_body("claude-sonnet-4-6")).status_code == 200
        assert (
            client.post(
                MESSAGES, json=_body("claude-sonnet-4-6"), headers={TOKEN_HEADER: "bad"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                MESSAGES, json=_body("claude-sonnet-4-6"), headers={TOKEN_HEADER: secret}
            ).status_code
            == 200
        )
    finally:
        client.__exit__(None, None, None)


def test_whoami(monkeypatch, tmp_path) -> None:
    client, upstream, path = _client(monkeypatch, tmp_path)
    try:
        secret = add_device("anand-macbook", path=path)
        good = client.get("/headroom/whoami", headers={TOKEN_HEADER: secret}).json()
        assert good["valid"] is True and good["device"] == "anand-macbook"
        assert "sha256" not in json.dumps(good)
        for headers in ({}, {TOKEN_HEADER: "hrt_bad"}):
            bad = client.get("/headroom/whoami", headers=headers).json()
            assert bad == {"valid": False, "device": None, "allow": []}
        upstream.post.assert_not_called()
    finally:
        client.__exit__(None, None, None)


def test_whoami_exempt_from_project_requirement(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HEADROOM_REQUIRE_PROJECT", "1")
    client, _, path = _client(monkeypatch, tmp_path)
    try:
        assert client.get("/headroom/whoami").status_code == 200
        # ...while a normal untagged request is still rejected by the project gate
        assert client.post(MESSAGES, json=_body("claude-sonnet-4-6")).status_code == 400
    finally:
        client.__exit__(None, None, None)
