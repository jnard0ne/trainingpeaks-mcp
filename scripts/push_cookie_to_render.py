#!/usr/bin/env python3
"""Push the locally stored TrainingPeaks cookie to a hosted tp-mcp on Render.

The hosted server (see docs/remote-deployment.md) reads its cookie from the
``TP_AUTH_COOKIE`` environment variable and cannot refresh it itself - there is
no browser on the server. This script closes that loop from a machine that
*does* have the cookie: it reads the credential the same way ``tp-mcp serve``
does (keyring, then encrypted file), optionally re-extracts it from a browser
first, validates it against TrainingPeaks, and writes it to the Render
service's environment through the Render API. Render redeploys the service
automatically when an environment variable changes.

The cookie value is never printed or logged.

Usage:
    RENDER_API_KEY=rnd_... python scripts/push_cookie_to_render.py <service-id>
    RENDER_API_KEY=rnd_... python scripts/push_cookie_to_render.py <service-id> --from-browser chrome

Find the service id (``srv-...``) in the Render dashboard URL for the service.
Create an API key at https://dashboard.render.com/u/settings#api-keys.
"""

import argparse
import os
import sys

import httpx

from tp_mcp.auth import get_credential, store_credential, validate_auth_sync
from tp_mcp.auth.browser import extract_tp_cookie

RENDER_API = "https://api.render.com/v1"
ENV_VAR_KEY = "TP_AUTH_COOKIE"


def _fail(message: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push the local TrainingPeaks cookie to a Render service.")
    parser.add_argument("service_id", help="Render service id, e.g. srv-abc123")
    parser.add_argument(
        "--from-browser",
        metavar="BROWSER",
        help="re-extract the cookie from a browser first (chrome, firefox, safari, edge, auto) and store it locally",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="also trigger a deploy explicitly (Render normally redeploys on its own when an env var changes)",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("RENDER_API_KEY", "").strip()
    if not api_key:
        return _fail("RENDER_API_KEY is not set.")

    if args.from_browser:
        browser = None if args.from_browser == "auto" else args.from_browser
        extracted = extract_tp_cookie(browser)
        if not extracted.success or not extracted.cookie:
            return _fail(f"browser extraction failed: {extracted.message}")
        print(f"Extracted cookie from {extracted.browser}.")
        cookie = extracted.cookie
    else:
        cred = get_credential()
        if not cred.success or not cred.cookie:
            return _fail("no local credential found. Run 'tp-mcp auth' or pass --from-browser.")
        cookie = cred.cookie

    print("Validating cookie against TrainingPeaks...")
    result = validate_auth_sync(cookie)
    if not result.is_valid:
        return _fail(f"cookie is not valid ({result.status.value}): {result.message}")
    print(f"Valid for {result.email} (athlete {result.athlete_id}).")

    if args.from_browser:
        stored = store_credential(cookie)
        if not stored.success:
            print(f"Warning: could not store cookie locally: {stored.message}", file=sys.stderr)

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    with httpx.Client(base_url=RENDER_API, headers=headers, timeout=30) as client:
        response = client.put(f"/services/{args.service_id}/env-vars/{ENV_VAR_KEY}", json={"value": cookie})
        if response.status_code >= 400:
            # Render error bodies never echo the value; safe to surface.
            return _fail(f"Render API returned {response.status_code}: {response.text[:300]}")
        print(f"Updated {ENV_VAR_KEY} on {args.service_id}.")

        if args.deploy:
            response = client.post(f"/services/{args.service_id}/deploys", json={})
            if response.status_code >= 400:
                return _fail(f"deploy trigger returned {response.status_code}: {response.text[:300]}")
            print("Deploy triggered.")
        else:
            print("Render will redeploy the service automatically to pick up the new value.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
