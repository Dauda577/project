import os
import sys
import json
import uuid
import hmac
import base64
import time
import logging
import hashlib
import secrets
import asyncio
from pathlib import Path
from datetime import datetime, timezone

if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")

from dotenv import load_dotenv

_BASE_DIR = Path(__file__).parent.resolve()
load_dotenv(_BASE_DIR / ".env")
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.requests import Request
from starlette.routing import Route

from mcp.server import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "server.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mcp-server")

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
if not MCP_AUTH_TOKEN:
    MCP_AUTH_TOKEN = hashlib.sha256(os.urandom(32)).hexdigest()
    env_path = Path(__file__).parent / ".env"
    with open(env_path, "a", encoding="utf-8") as f:
        f.write(f'\nMCP_AUTH_TOKEN="{MCP_AUTH_TOKEN}"\n')
    logger.info("Generated new MCP_AUTH_TOKEN and wrote to .env")

AUTH_CODE_EXPIRY = 300

_oauth_clients: dict[str, dict] = {}
_oauth_codes: dict[str, dict] = {}

mcp = FastMCP(
    "local-mcp-v2",
    host="127.0.0.1",
    port=8000,
    streamable_http_path="/mcp",
    log_level="INFO",
    transport_security=TransportSecuritySettings(
    allowed_hosts=["127.0.0.1", "localhost", "mcp.sneakershub.site", "127.0.0.1:8000", "localhost:8000"],
),
)

import tools
tools.register_all(mcp)


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": "https://mcp.sneakershub.site",
        "authorization_endpoint": "https://mcp.sneakershub.site/authorize",
        "token_endpoint": "https://mcp.sneakershub.site/token",
        "registration_endpoint": "https://mcp.sneakershub.site/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
        "code_challenge_methods_supported": ["S256"],
    })


_OAUTH_PROTECTED_RESOURCE_BODY = {
    "resource": "https://mcp.sneakershub.site/mcp",
    "authorization_servers": ["https://mcp.sneakershub.site"],
}

@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse(_OAUTH_PROTECTED_RESOURCE_BODY)

@mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
async def oauth_protected_resource_mcp(request: Request) -> JSONResponse:
    return JSONResponse(_OAUTH_PROTECTED_RESOURCE_BODY)


@mcp.custom_route("/register", methods=["GET", "POST"])
async def register_client(request: Request) -> JSONResponse:
    if request.method == "GET":
        return JSONResponse({
            "registration_endpoint": "https://mcp.sneakershub.site/register",
        })
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = secrets.token_urlsafe(32)
    client_secret = secrets.token_urlsafe(48)
    redirect_uris = body.get("redirect_uris", [])
    client_name = body.get("client_name", "unnamed")
    _oauth_clients[client_id] = {
        "client_secret": client_secret,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "created_at": time.time(),
    }
    logger.info(f"Registered OAuth client: {client_id}")
    return JSONResponse(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_basic",
        },
        status_code=201,
    )


@mcp.custom_route("/authorize", methods=["GET"])
async def authorize_form(request: Request) -> HTMLResponse:
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Authorize MCP Server</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh; margin: 0;
    }}
    .card {{
      background: white; border-radius: 12px; padding: 2rem;
      box-shadow: 0 2px 16px rgba(0,0,0,0.1); max-width: 420px; width: 100%;
    }}
    h1 {{ font-size: 1.4rem; margin: 0 0 0.5rem; }}
    p {{ color: #666; margin: 0 0 1.5rem; font-size: 0.9rem; }}
    label {{ display: block; font-weight: 600; margin-bottom: 0.4rem; font-size: 0.85rem; }}
    input[type=password] {{
      width: 100%; padding: 0.7rem; border: 1px solid #ddd; border-radius: 6px;
      font-size: 1rem; box-sizing: border-box; margin-bottom: 1rem;
    }}
    button {{
      width: 100%; padding: 0.7rem; background: #0051ff; color: white;
      border: none; border-radius: 6px; font-size: 1rem; cursor: pointer;
    }}
    button:hover {{ background: #003fd6; }}
    .error {{ color: #d32f2f; margin-bottom: 1rem; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Authorize Access</h1>
    <p>A client is requesting access to your MCP server. Enter your access token to authorize.</p>
    <form method="POST" action="/authorize">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <label for="token">Access Token</label>
      <input type="password" id="token" name="token" placeholder="Enter your access token" autocomplete="off">
      <button type="submit">Authorize</button>
    </form>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@mcp.custom_route("/authorize", methods=["POST"])
async def handle_authorize_submit(request: Request):
    form = await request.form()
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    submitted_token = form.get("token", "")

    if client_id not in _oauth_clients:
        return HTMLResponse(
            "<html><body><h1>401 Unauthorized</h1><p>Unknown client.</p></body></html>",
            status_code=401,
        )

    registered = _oauth_clients[client_id]
    if redirect_uri and registered.get("redirect_uris"):
        if redirect_uri not in registered["redirect_uris"]:
            return HTMLResponse(
                "<html><body><h1>400 Bad Request</h1><p>redirect_uri does not match registered URIs.</p></body></html>",
                status_code=400,
            )

    if not submitted_token:
        return HTMLResponse(
            "<html><body><h1>401 Unauthorized</h1><p>Access token is required.</p></body></html>",
            status_code=401,
        )

    expected = MCP_AUTH_TOKEN.encode("utf-8")
    actual = submitted_token.encode("utf-8")
    if not secrets.compare_digest(expected, actual):
        logger.warning(f"Failed authorize attempt for client {client_id}")
        return HTMLResponse(
            "<html><body><h1>401 Unauthorized</h1><p>Invalid access token.</p></body></html>",
            status_code=401,
        )

    code = secrets.token_urlsafe(32)
    code_challenge = form.get("code_challenge", "")
    code_challenge_method = form.get("code_challenge_method", "")
    _oauth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "created_at": time.time(),
    }
    logger.info(f"Minted auth code for client {client_id} (challenge_method={code_challenge_method})")

    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}"
        if state:
            location += f"&state={state}"
        return Response(status_code=302, headers={"Location": location})

    return JSONResponse({"code": code, "state": state})


@mcp.custom_route("/token", methods=["POST"])
async def token_exchange(request: Request):
    content_type = request.headers.get("content-type", "")
    logger.info(f"POST /token — Content-Type: {content_type}")

    body = {}
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        for key in form:
            body[key] = form.get(key)
        logger.info(f"POST /token — form fields: {dict(body)}")
    else:
        try:
            body = await request.json()
            logger.info(f"POST /token — JSON body: {body}")
        except Exception:
            logger.warning("POST /token — could not parse body as JSON")

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    client_id = body.get("client_id", "")
    client_secret = body.get("client_secret", "")
    redirect_uri = body.get("redirect_uri", "")
    code_verifier = body.get("code_verifier", "")

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic ") and not client_id:
        import base64 as b64
        try:
            decoded = b64.b64decode(auth_header[6:]).decode("utf-8")
            client_id, client_secret = decoded.split(":", 1)
        except Exception:
            pass

    if grant_type != "authorization_code":
        logger.warning(f"POST /token — unsupported grant_type: {grant_type!r}")
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    if not code:
        logger.warning("POST /token — missing code")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if code not in _oauth_codes:
        logger.warning(f"POST /token — unknown code (not in store)")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    code_data = _oauth_codes[code]
    if time.time() - code_data["created_at"] > AUTH_CODE_EXPIRY:
        del _oauth_codes[code]
        logger.warning("POST /token — code expired")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if client_id and code_data["client_id"] != client_id:
        logger.warning(f"POST /token — client_id mismatch: got {client_id!r}, expected {code_data['client_id']!r}")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if redirect_uri and code_data.get("redirect_uri") and redirect_uri != code_data["redirect_uri"]:
        logger.warning(f"POST /token — redirect_uri mismatch: got {redirect_uri!r}, expected {code_data['redirect_uri']!r}")
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if client_id and client_secret:
        stored = _oauth_clients.get(client_id, {})
        expected = stored.get("client_secret", "")
        if not secrets.compare_digest(expected.encode("utf-8"), client_secret.encode("utf-8")):
            logger.warning(f"POST /token — client_secret mismatch for client {client_id}")
            return JSONResponse({"error": "invalid_client"}, status_code=401)

    stored_challenge = code_data.get("code_challenge", "")
    stored_challenge_method = code_data.get("code_challenge_method", "")
    if stored_challenge:
        if not code_verifier:
            logger.warning("POST /token — PKCE required but code_verifier missing")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if stored_challenge_method == "S256":
            computed = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest()).rstrip(b"=").decode("utf-8")
            if not secrets.compare_digest(computed, stored_challenge):
                logger.warning("POST /token — PKCE verification failed (S256 mismatch)")
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        elif stored_challenge_method == "plain":
            if not secrets.compare_digest(code_verifier, stored_challenge):
                logger.warning("POST /token — PKCE verification failed (plain mismatch)")
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
        elif stored_challenge_method:
            logger.warning(f"POST /token — unknown code_challenge_method: {stored_challenge_method!r}")
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    del _oauth_codes[code]

    logger.info(f"Token exchange succeeded for client {code_data['client_id']}")

    return JSONResponse(
        {
            "access_token": MCP_AUTH_TOKEN,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/mcp":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    {"error": "unauthorized", "message": "Missing or invalid Authorization header"},
                    status_code=401,
                )
            token = auth_header[len("Bearer "):]
            expected = MCP_AUTH_TOKEN.encode("utf-8")
            actual = token.encode("utf-8")
            if not secrets.compare_digest(expected, actual):
                return JSONResponse(
                    {"error": "unauthorized", "message": "Invalid token"},
                    status_code=401,
                )
        response = await call_next(request)
        return response


@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request: Request) -> Response:
    return Response(
        status_code=307,
        headers={"Location": "/mcp"},
    )


app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)


def main():
    import uvicorn
    logger.info(f"Starting MCP server on 127.0.0.1:8000/mcp")
    logger.info(f"ROOT_DIR: {tools.ROOT_DIR}")
    logger.info(f"MCP_AUTH_TOKEN: {MCP_AUTH_TOKEN[:12]}...")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
