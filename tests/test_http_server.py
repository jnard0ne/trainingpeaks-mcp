"""Tests for the Streamable HTTP transport and its shared-secret auth."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from tp_mcp.http_server import (
    MCP_PATH,
    TOKEN_ENV_VAR,
    MissingAuthTokenError,
    create_app,
    main,
    resolve_token,
)

TOKEN = "unit-test-shared-secret-0123456789"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _rpc(method: str, params: dict | None = None, id_: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


@pytest.fixture
def client():
    app = create_app(TOKEN, validate_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


class TestTokenResolution:
    def test_explicit_token_wins(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
        assert resolve_token("explicit") == "explicit"

    def test_env_token_used_when_no_argument(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "  from-env  ")
        assert resolve_token() == "from-env"

    def test_missing_token_refuses_to_start(self, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        with pytest.raises(MissingAuthTokenError):
            create_app(validate_on_startup=False)

    def test_blank_token_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv(TOKEN_ENV_VAR, "   ")
        with pytest.raises(MissingAuthTokenError):
            create_app(validate_on_startup=False)

    def test_main_exits_nonzero_without_token(self, monkeypatch):
        monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
        assert main(["--port", "1"]) == 1


class TestPublicEndpoints:
    def test_index_describes_service(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert response.json()["mcp_endpoint"] == MCP_PATH
        assert TOKEN not in response.text

    def test_health_without_credential(self, client, monkeypatch):
        monkeypatch.delenv("TP_AUTH_COOKIE", raising=False)
        with patch("tp_mcp.http_server.get_credential") as get_cred:
            get_cred.return_value.success = False
            get_cred.return_value.cookie = None
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "credential_configured": False}

    def test_health_with_credential(self, client, env_credential):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "credential_configured": True}
        assert "test_cookie" not in response.text


class TestAuthGate:
    def test_missing_token_is_401(self, client):
        response = client.post(MCP_PATH, headers=MCP_HEADERS, json=_rpc("tools/list"))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_wrong_bearer_is_401(self, client):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}x"}
        response = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
        assert response.status_code == 401

    def test_wrong_scheme_is_401(self, client):
        headers = {**MCP_HEADERS, "Authorization": f"Basic {TOKEN}"}
        response = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
        assert response.status_code == 401

    def test_wrong_query_token_is_401(self, client):
        response = client.post(f"{MCP_PATH}?token=nope", headers=MCP_HEADERS, json=_rpc("tools/list"))
        assert response.status_code == 401

    def test_get_without_token_is_401(self, client):
        response = client.get(MCP_PATH, headers={"Accept": "text/event-stream"})
        assert response.status_code == 401


class TestMcpOverHttp:
    def _tool_names(self, response) -> set[str]:
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["jsonrpc"] == "2.0"
        return {tool["name"] for tool in payload["result"]["tools"]}

    def test_bearer_header_lists_tools(self, client):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        response = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
        names = self._tool_names(response)
        assert {"tp_get_workouts", "tp_auth_status", "tp_get_fitness"} <= names

    def test_api_key_header_lists_tools(self, client):
        headers = {**MCP_HEADERS, "X-API-Key": TOKEN}
        response = client.post(MCP_PATH, headers=headers, json=_rpc("tools/list"))
        assert "tp_get_workouts" in self._tool_names(response)

    def test_query_token_lists_tools(self, client):
        response = client.post(f"{MCP_PATH}?token={TOKEN}", headers=MCP_HEADERS, json=_rpc("tools/list"))
        assert "tp_get_workouts" in self._tool_names(response)

    def test_initialize_is_stateless(self, client):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        }
        response = client.post(MCP_PATH, headers=headers, json=_rpc("initialize", params))
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "trainingpeaks-mcp"
        # Stateless mode: no session to carry between requests.
        assert "mcp-session-id" not in {k.lower() for k in response.headers}

    def test_tool_call_dispatches_to_handlers(self, client):
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
        with patch("tp_mcp.server.tp_get_workout_types", new=AsyncMock(return_value={"families": []})):
            response = client.post(
                MCP_PATH,
                headers=headers,
                json=_rpc("tools/call", {"name": "tp_get_workout_types", "arguments": {}}),
            )
        assert response.status_code == 200, response.text
        content = response.json()["result"]["content"]
        assert json.loads(content[0]["text"]) == {"families": []}
