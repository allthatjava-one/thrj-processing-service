"""
api-gateway — Cloudflare Python Worker
POST /api/v1/pdf-compressor
"""
from workers import WorkerEntrypoint, Response

import asyncio
import json
import time
from urllib.parse import urlparse
from js import AbortController, Object, Response, clearTimeout, fetch as js_fetch, setTimeout
from pyodide.ffi import to_js

# ---------------------------------------------------------------------------
# Allowed origins are loaded from the ALLOWED_ORIGINS environment variable.
# Set it as a comma-separated string in .dev.vars, wrangler.json vars, or via secret:
#   ALLOWED_ORIGINS = "http://localhost:4173,https://pdf-compressor.thrjtech.com"
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_COMPRESS_WAIT_SECONDS = 90
DEFAULT_INITIAL_RETRY_DELAY_SECONDS = 1
DEFAULT_COMPRESSED_PDF_FETCH_TIMEOUT_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 5

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


async def _call_compress_service_with_retry(
    external_url: str,
    object_key: str,
    compressed_pdf_fetch_timeout_seconds: float,
):
    deadline = time.monotonic() + MAX_COMPRESS_WAIT_SECONDS
    attempt = 0
    retry_delay = DEFAULT_INITIAL_RETRY_DELAY_SECONDS
    last_error = "Compress service did not become ready in time."

    while time.monotonic() < deadline:
        attempt += 1
        ext_resp, fetch_error = await _fetch_compress_with_timeout(
            external_url,
            object_key,
            compressed_pdf_fetch_timeout_seconds,
        )
        if ext_resp is None:
            last_error = fetch_error

        if ext_resp is not None:
            if ext_resp.ok:
                return ext_resp, ""

            body_text = await ext_resp.text()
            if ext_resp.status not in TRANSIENT_STATUS_CODES:
                return None, f"Compress service returned {ext_resp.status}: {body_text[:200]}"

            last_error = (
                f"Compress service temporary failure ({ext_resp.status}): {body_text[:200]}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        sleep_for = min(retry_delay, remaining)
        print(
            f"[pdf-compressor] attempt {attempt} failed, retrying in {sleep_for:.1f}s"
        )
        await asyncio.sleep(sleep_for)
        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY_SECONDS)

    return None, last_error


async def _fetch_compress_with_timeout(
    external_url: str,
    object_key: str,
    compressed_pdf_fetch_timeout_seconds: float,
):
    controller = AbortController.new()
    timeout_ms = max(1, int(compressed_pdf_fetch_timeout_seconds * 1000))
    timeout_id = setTimeout(controller.abort, timeout_ms)

    try:
        ext_resp = await js_fetch(
            external_url,
            to_js(
                {
                    "method": "POST",
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"objectKey": object_key}),
                    "signal": controller.signal,
                },
                dict_converter=Object.fromEntries,
            ),
        )
        return ext_resp, ""
    except Exception as exc:
        error_name = getattr(exc, "name", "")
        error_text = str(exc)
        if error_name == "AbortError" or "AbortError" in error_text:
            return None, f"Compress service request timed out after {compressed_pdf_fetch_timeout_seconds}s."
        return None, f"Failed to call compress service: {exc}"
    finally:
        clearTimeout(timeout_id)


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

    compressed_pdf_fetch_timeout_seconds = DEFAULT_COMPRESSED_PDF_FETCH_TIMEOUT_SECONDS
    fetch_timeout_raw = getattr(env, "COMPRESSED_PDF_FETCH_TIMEOUT_SECONDS", None)
    if fetch_timeout_raw is not None:
        try:
            configured_timeout = float(fetch_timeout_raw)
            if configured_timeout > 0:
                compressed_pdf_fetch_timeout_seconds = configured_timeout
        except (TypeError, ValueError):
            print(
                "[pdf-compressor] invalid COMPRESSED_PDF_FETCH_TIMEOUT_SECONDS; using default"
            )

    print(f"[pdf-compressor] calling external compress service for: {object_key}")
    ext_resp, call_error = await _call_compress_service_with_retry(
        external_url,
        object_key,
        compressed_pdf_fetch_timeout_seconds,
    )
    if ext_resp is None:
        print(f"[pdf-compressor] external service unavailable: {call_error}")
        return _error(502, call_error, origin)

    print(f"[pdf-compressor] external response status: {ext_resp.status}")

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
