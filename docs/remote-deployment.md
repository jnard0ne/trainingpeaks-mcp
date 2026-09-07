# Remote deployment (Render)

Running the server on your own machine means it goes away when the machine
sleeps. This fork adds an opt-in **Streamable HTTP** transport
(`src/tp_mcp/http_server.py`) and a `render.yaml` that hosts it as a small,
always-reachable web service behind a shared secret. Nothing about the default
`tp-mcp serve` stdio mode changes.

## Deploy

1. Render dashboard -> **New** -> **Blueprint** -> select this repo. Render
   creates a `trainingpeaks-mcp` Python web service and generates
   `TP_MCP_AUTH_TOKEN` for you.
2. When prompted, set `TP_AUTH_COOKIE` to your `Production_tpAuth` cookie value
   (README, "Step 2: Authenticate", Option B/C). The hosted server has no
   keyring and an ephemeral disk, so the environment variable is the only
   credential source.
3. After the first deploy, confirm `https://<service>.onrender.com/health`
   returns `{"status": "ok", "credential_configured": true}`.
4. Copy `TP_MCP_AUTH_TOKEN` from the service's **Environment** tab. Every
   client presents it as `Authorization: Bearer <token>`.

The MCP endpoint is `https://<service>.onrender.com/mcp`.

> **Free plan caveat:** free Render services spin down after ~15 minutes idle
> and take up to a minute to wake, which can trip an MCP client's connect
> timeout on the first call. Either change `plan: free` to `starter` in
> `render.yaml`, or point a free uptime monitor at `/health` every 10 minutes.

## Connect clients

**Claude Code** (any machine, or Claude Code on the web):

```bash
claude mcp add --transport http trainingpeaks https://<service>.onrender.com/mcp \
  --header "Authorization: Bearer <TP_MCP_AUTH_TOKEN>"
```

Or in a `.mcp.json`, keeping the secret in the environment:

```json
{
  "mcpServers": {
    "trainingpeaks": {
      "type": "http",
      "url": "https://<service>.onrender.com/mcp",
      "headers": { "Authorization": "Bearer ${TP_MCP_AUTH_TOKEN}" }
    }
  }
}
```

**claude.ai / Claude Desktop custom connector:** the connector UI has no header
field, so put the token in the URL instead:
`https://<service>.onrender.com/mcp?token=<TP_MCP_AUTH_TOKEN>`. Treat that URL
as a secret.

**Any other MCP client:** Streamable HTTP at `/mcp`, with the token as
`Authorization: Bearer`, `X-API-Key`, or `?token=`.

## Keeping the cookie fresh

TrainingPeaks session cookies expire after a few weeks. When `tp_auth_status`
reports the session has expired, the hosted server needs a fresh
`Production_tpAuth` value in `TP_AUTH_COOKIE`. `tp_refresh_auth` cannot help
here because there is no browser on the server.

From any machine that is logged into TrainingPeaks, run:

```bash
RENDER_API_KEY=rnd_... python scripts/push_cookie_to_render.py srv-<service-id> --from-browser chrome
```

It extracts the cookie from the browser, validates it, stores it locally for
`tp-mcp serve`, and writes it to the service's environment through the Render
API (the value is never printed). Render redeploys automatically. Omit
`--from-browser` to push the cookie already stored by `tp-mcp auth`. The
service id is the `srv-...` segment of the service's dashboard URL; create an
API key under Account Settings -> API Keys.

Pasting the value into the Render dashboard by hand works too.

## Security notes

- Every request to `/mcp` must carry the shared secret, compared in constant
  time. The server refuses to start without one.
- Only `/` and `/health` are unauthenticated, and neither touches TrainingPeaks
  data. `/health` reports whether a cookie is configured, never its value.
- Anyone holding the token has the same read/write access to your calendar
  that Claude does. Treat it like a password and rotate it from the Render
  dashboard if it leaks.
- The SDK's DNS-rebinding Host-header check is disabled because it is designed
  for localhost servers; behind Render's proxy the token is the access control.

## Run the HTTP transport locally

```bash
TP_MCP_AUTH_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  python -m tp_mcp.http_server --port 8000
```

## Staying current with upstream

`.github/workflows/sync-upstream.yml` merges
[JamsusMaximus/trainingpeaks-mcp](https://github.com/JamsusMaximus/trainingpeaks-mcp)
`main` into this fork's `main` daily (and on demand from the Actions tab).
Every push to `main` redeploys the Render service. The fork only adds new
files (this doc, `http_server.py`, its tests, `render.yaml`, the workflow), so
merges should stay conflict-free; if upstream ever touches one of those paths
the workflow fails loudly and the merge needs a manual resolution.

GitHub disables scheduled workflows in forks until you enable them once:
**Actions** tab -> "Sync upstream" -> **Enable workflow**.
