"""Streamable HTTP transport for running tp-mcp as a remote service.

``tp-mcp serve`` speaks MCP over stdio, which only works when the client runs
on the same machine. ``python -m tp_mcp.http_server`` exposes the very same
server over the MCP Streamable HTTP transport so a hosted copy (e.g. on
Render) can be reached from Claude Code, claude.ai, or any other MCP client,
from anywhere.

This module is deliberately self-contained (its own entry point, no changes
to ``cli.py``) so a fork carrying it can keep merging upstream cleanly.

Endpoints:
    GET  /          Service description (no auth).
    GET  /health    Liveness probe for the host platform (no auth).
    POST /mcp       MCP Streamable HTTP endpoint (auth required).

Authentication is a single shared secret read from ``TP_MCP_AUTH_TOKEN``.
Clients present it as ``Authorization: Bearer <token>`` (preferred), as an
``X-API-Key`` header, or - for clients that cannot set headers - as a
``?token=<token>`` query parameter on the endpoint URL. The server refuses
to start without a token so a misconfigured deploy fails closed.

The TrainingPeaks cookie itself comes from ``TP_AUTH_COOKIE`` (see
``tp_mcp.auth.storage``); hosted environments have no keyring and an
ephemeral disk, so the environment variable is the only durable option.
"""

import argparse
import contextlib
import hmac
import logging
import os
import sys
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from tp_mcp.auth import get_credential

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "TP_MCP_AUTH_TOKEN"
MCP_PATH = "/mcp"
DEFAULT_HOST = "0.0.0.0"  # a hosted service must bind all interfaces
DEFAULT_PORT = 8000
MIN_TOKEN_LENGTH = 16


class MissingAuthTokenError(RuntimeError):
    """Raised when the HTTP server is started without a shared secret."""


def _presented_token(scope: Scope) -> str | None:
    """Extract the client's credential from an incoming request, if any."""
    headers = Headers(scope=scope)

    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    api_key = headers.get("x-api-key", "")
    if api_key.strip():
        return api_key.strip()

    query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
    values = query.get("token")
    if values and values[0].strip():
        return values[0].strip()

    return None


class SharedSecretAuth:
    """ASGI wrapper that rejects requests lacking the shared secret."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self._token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        presented = _presented_token(scope)
        if presented is None or not hmac.compare_digest(presented.encode("utf-8"), self._token):
            response = JSONResponse(
                {"error": "unauthorized", "message": "Missing or invalid token."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="trainingpeaks-mcp"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def resolve_token(token: str | None = None) -> str:
    """Pick the shared secret from the argument or the environment.

    Raises:
        MissingAuthTokenError: if no non-empty token is available.
    """
    value = (token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")).strip()
    if not value:
        raise MissingAuthTokenError(
            f"{TOKEN_ENV_VAR} is not set. The HTTP transport refuses to run without a shared "
            "secret; generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
        )
    if len(value) < MIN_TOKEN_LENGTH:
        logger.warning("%s is shorter than %d characters; use a longer random secret.", TOKEN_ENV_VAR, MIN_TOKEN_LENGTH)
    return value


async def _health(_: Request) -> JSONResponse:
    cred = get_credential()
    return JSONResponse({"status": "ok", "credential_configured": bool(cred.success and cred.cookie)})


async def _index(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "trainingpeaks-mcp",
            "transport": "streamable-http",
            "mcp_endpoint": MCP_PATH,
            "auth": "Authorization: Bearer <TP_MCP_AUTH_TOKEN>",
        }
    )


def create_app(token: str | None = None, *, validate_on_startup: bool = True) -> Starlette:
    """Build the ASGI application.

    Args:
        token: Shared secret. Defaults to ``TP_MCP_AUTH_TOKEN``.
        validate_on_startup: Check the TrainingPeaks cookie when the app boots
            (log only; the server still starts so the deploy can be inspected).
    """
    # Imported lazily so ``tp_mcp.server`` (and its tool modules) only load
    # when the HTTP app is actually built.
    from tp_mcp.server import _validate_auth_on_startup, server

    secret = resolve_token(token)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        # One-shot JSON responses and no server-side sessions: survives restarts,
        # load balancers, and clients that reconnect after the host spins down.
        json_response=True,
        stateless=True,
        # Host-header pinning is meant for localhost servers; a hosted service
        # sits behind the platform's proxy and is protected by the token instead.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if validate_on_startup:
            await _validate_auth_on_startup()
        async with session_manager.run():
            logger.info("TrainingPeaks MCP HTTP transport ready at %s", MCP_PATH)
            yield

    return Starlette(
        routes=[
            Route("/", _index, methods=["GET"]),
            Route("/health", _health, methods=["GET"]),
            Route(MCP_PATH, SharedSecretAuth(handle_mcp, secret), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


def run_http_server(host: str | None = None, port: int | None = None) -> int:
    """Serve the MCP app with uvicorn."""
    import uvicorn

    bind_host = host or os.environ.get("HOST", DEFAULT_HOST)
    bind_port = port if port is not None else int(os.environ.get("PORT", DEFAULT_PORT))

    try:
        app = create_app()
    except MissingAuthTokenError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Starting TrainingPeaks MCP HTTP server on %s:%d", bind_host, bind_port)
    config: dict[str, Any] = {
        "host": bind_host,
        "port": bind_port,
        "log_level": "info",
        # Trust X-Forwarded-* from the platform's reverse proxy.
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
    }
    uvicorn.run(app, **config)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m tp_mcp.http_server``."""
    parser = argparse.ArgumentParser(
        prog="python -m tp_mcp.http_server",
        description="Serve the TrainingPeaks MCP server over Streamable HTTP.",
    )
    parser.add_argument("--host", default=None, help=f"bind address (default: $HOST or {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=None, help=f"bind port (default: $PORT or {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_http_server(host=args.host, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
