"""
api-gateway — Cloudflare Python Worker
POST /api/v1/pdf-compressor
"""
from workers import WorkerEntrypoint, Response

import json
from urllib.parse import urlparse
from js import Object, Response, fetch as js_fetch
from pyodide.ffi import to_js

# ---------------------------------------------------------------------------
# Allowed origins are loaded from the ALLOWED_ORIGINS environment variable.
# Set it as a comma-separated string in wrangler.toml [vars] or via secret:
#   ALLOWED_ORIGINS = "http://localhost:4173,https://pdf-compressor.thrjtech.com"
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_cors_headers(resp, origin: str):
    resp.headers.set("Access-Control-Allow-Origin", origin)
    resp.headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    resp.headers.set("Access-Control-Allow-Headers", "Content-Type")
    resp.headers.set("Vary", "Origin")


def _json_response(data: dict, status: int = 200, origin: str = "") -> Response:
    # Response.new(body) always creates 200; clone with new status when needed.
    init = {"status": status}
    resp = Response.new(json.dumps(data), init)
    resp.headers.set("Content-Type", "application/json")
    if origin:
        _set_cors_headers(resp, origin)
    return resp


def _error(status: int, message: str, origin: str = "") -> Response:
    return _json_response({"error": message}, status, origin)


def _handle_preflight(request, allowed_origins: list) -> Response:
    origin = request.headers.get("Origin") or ""
    if origin not in allowed_origins:
        return Response.new("Forbidden", {"status": 403})
    resp = Response.new("", {"status": 204})
    resp.headers.set("Access-Control-Allow-Origin", origin)
    resp.headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    resp.headers.set("Access-Control-Allow-Headers", "Content-Type")
    resp.headers.set("Access-Control-Max-Age", "86400")
    resp.headers.set("Vary", "Origin")
    return resp


# (R2 access removed) Use external compress service instead


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_hello(origin: str) -> Response:
    return _json_response({"message": "Hello World"}, 200, origin)


async def _handle_pdf_compressor(request, env, origin: str) -> Response:
    # Parse body
    try:
        body = await request.json()
    except Exception:
        return _error(400, "Request body must be valid JSON.", origin)

    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object.", origin)

    object_key = body.get("object_key")
    if not object_key:
        return _error(400, "Missing required field: object_key.", origin)

    if not isinstance(object_key, str):
        return _error(400, "object_key must be a string.", origin)
    # Call external compress service which returns a presigned key
    # external_url = "http://localhost:8787/compress"
    external_url = env.SERVICE_PDF_COMPRESS_URL
    print(f"[pdf-compressor] calling external compress service for: {object_key}")
    try:
        ext_resp = await js_fetch(
            external_url,
            to_js(
                {
                    "method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"objectKey": object_key}),
                },
                dict_converter=Object.fromEntries,
            ),
        )
    except Exception as exc:
        print(f"[pdf-compressor] external fetch exception: {exc}")
        return _error(502, f"Failed to call compress service: {exc}", origin)

    print(f"[pdf-compressor] external response status: {ext_resp.status}")
    if not ext_resp.ok:
        body_text = await ext_resp.text()
        print(f"[pdf-compressor] external error body: {body_text[:200]}")
        return _error(502, f"Compress service returned {ext_resp.status}: {body_text[:200]}", origin)

    try:
        result_text = await ext_resp.text()
        result = json.loads(result_text)
    except Exception as exc:
        print(f"[pdf-compressor] invalid json from compress service: {exc}")
        return _error(502, "Compress service returned invalid JSON.", origin)

    presigned = result.get("presignedUrl")
    if not presigned:
        return _error(502, "Compress service did not return presignedUrl.", origin)

    return _json_response({"presignedUrl": presigned}, 200, origin)


# ---------------------------------------------------------------------------
# Cloudflare Workers entry point
# ---------------------------------------------------------------------------
class Default(WorkerEntrypoint):
    async def on_fetch(self, request):
        env = self.env
        method = request.method.upper()

        raw = getattr(env, "ALLOWED_ORIGINS", "") or ""
        allowed_origins = [o.strip() for o in raw.split(",") if o.strip()]

        # CORS preflight
        if method == "OPTIONS":
            return _handle_preflight(request, allowed_origins)

        # Origin check — all non-preflight requests must come from an allowed origin
        origin = request.headers.get("Origin") or ""
        if origin not in allowed_origins:
            return _error(403, "Forbidden: Origin not allowed.")

        path = urlparse(str(request.url)).path.rstrip("/")

        if path == "/api/v1/hello":
            if method == "GET":
                return _handle_hello(origin)
            return _error(405, "Method Not Allowed: use GET.", origin)

        if path == "/api/v1/pdf-compressor":
            if method == "POST":
                return await _handle_pdf_compressor(request, env, origin)
            return _error(405, "Method Not Allowed: use POST.", origin)

        return _error(404, "Not Found.", origin)
