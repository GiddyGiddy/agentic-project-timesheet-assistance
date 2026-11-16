import json
from types import SimpleNamespace

import pytest

from src.integrations.mcp_client import MCPClient
from src.integrations.workiq_auth import WorkIQAuth, WORKIQ_URL


class FakeApp:
    def __init__(self, **kwargs):
        self.accounts = []

    def acquire_token_interactive(self, scopes, timeout):
        assert timeout == 300
        assert any(scope.endswith("/WorkIQAgent.Ask") for scope in scopes)
        self.accounts = [{"id": "account"}]
        return {"access_token": "private-access-token"}

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        return {"access_token": "private-access-token"}


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr("src.integrations.workiq_auth.PublicClientApplication", FakeApp)
    return WorkIQAuth("fd58bbca-b54d-436b-a7bc-3cb0e2c1181a", "725cf584-2567-4a8f-b855-866ca3b94575")


def test_browser_sign_in_keeps_token_in_process(auth):
    with pytest.raises(ValueError, match="Sign in"):
        auth.token()
    assert auth.sign_in() is None
    assert auth.token() == "private-access-token"


def test_failed_flow_can_be_restarted(auth, monkeypatch):
    monkeypatch.setattr(auth.app, "acquire_token_interactive", lambda **kwargs: {"error": "authorization_declined"})
    with pytest.raises(ValueError, match="authorization_declined"):
        auth.sign_in()
    with pytest.raises(ValueError, match="authorization_declined"):
        auth.sign_in()


def test_concurrent_sign_in_is_rejected(auth):
    auth.sign_in_lock.acquire()
    try:
        with pytest.raises(ValueError, match="already in progress"):
            auth.sign_in()
    finally:
        auth.sign_in_lock.release()


async def test_workiq_token_only_sent_to_official_endpoint(auth, tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "servers": {"workiq": {"transport": "http", "url": WORKIQ_URL, "auth_type": "workiq_browser"}},
        "operations": {},
    }), encoding="utf-8")
    auth.sign_in()
    monkeypatch.setattr("src.integrations.mcp_client.get_workiq_auth", lambda: auth)
    captured = []

    class Client:
        def __init__(self, servers, **kwargs):
            captured.append(servers)

        async def get_tools(self, server_name):
            return [SimpleNamespace(name="fetch")]

    monkeypatch.setattr("src.integrations.mcp_client.MultiServerMCPClient", Client)
    client = MCPClient(config)
    assert (await client.tools("workiq"))[0].name == "fetch"
    assert captured[-1]["workiq"]["headers"]["Authorization"] == "Bearer private-access-token"
    config.write_text(json.dumps({
        "servers": {"workiq": {"transport": "http", "url": "https://evil.example/mcp", "auth_type": "workiq_browser"}},
        "operations": {},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="official HTTPS"):
        MCPClient(config)
