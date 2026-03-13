"""
thrj-processor-service — Cloudflare Python Worker
POST /api/v1/pdf-compressor
"""
from workers import WorkerEntrypoint, Response

import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from js import Object, Response, fetch as js_fetch
from pyodide.ffi import to_js

# ---------------------------------------------------------------------------
# Allowed origins — update these with your actual frontend URLs before deploy
# ---------------------------------------------------------------------------
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "https://app.yourdomain.com",
    # Add or remove origins as needed
]

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


def _handle_preflight(request) -> Response:
    origin = request.headers.get("Origin") or ""
    if origin not in ALLOWED_ORIGINS:
        return Response.new("Forbidden", {"status": 403})
    resp = Response.new("", {"status": 204})
    resp.headers.set("Access-Control-Allow-Origin", origin)
    resp.headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    resp.headers.set("Access-Control-Allow-Headers", "Content-Type")
    resp.headers.set("Access-Control-Max-Age", "86400")
    resp.headers.set("Vary", "Origin")
    return resp


def _extract_base_filename(object_key: str) -> str:
    """Return the filename stem (no extension) from an R2 object key."""
    last = object_key.rstrip("/").split("/")[-1]
    if last.lower().endswith(".pdf"):
        return last[:-4]
    if "." in last:
        return last.rsplit(".", 1)[0]
    return last or "file"


# ---------------------------------------------------------------------------
# AWS SigV4 helpers (used to authenticate requests to R2 S3-compatible API)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _build_r2_auth_headers(
    object_key: str,
    endpoint_host: str,
    access_key_id: str,
    secret_access_key: str,
    bucket_name: str,
) -> dict:
    region = "auto"
    service = "s3"
    host = endpoint_host

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    # URI-encode each path segment of the key
    encoded_key = "/".join(quote(seg, safe="") for seg in object_key.split("/"))
    canonical_uri = f"/{bucket_name}/{encoded_key}"

    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        "GET",
        canonical_uri,
        "",  # no query string
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _hmac_sha256(
        _hmac_sha256(
            _hmac_sha256(
                _hmac_sha256(
                    ("AWS4" + secret_access_key).encode("utf-8"),
                    date_stamp,
                ),
                region,
            ),
            service,
        ),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
    }


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

    # Derive output filename
    base_name = _extract_base_filename(object_key)
    compressed_filename = f"{base_name}-compress.pdf"

    # Read R2 credentials from environment variables
    endpoint_url = env.R2_ENDPOINT_URL.rstrip("/")   # e.g. https://<account>.r2.cloudflarestorage.com
    endpoint_host = urlparse(endpoint_url).netloc
    access_key_id = env.R2_ACCESS_KEY_ID
    secret_access_key = env.R2_SECRET_ACCESS_KEY
    bucket_name = env.R2_BUCKET_NAME

    url = f"{endpoint_url}/{bucket_name}/{object_key}"

    headers = _build_r2_auth_headers(
        object_key, endpoint_host, access_key_id, secret_access_key, bucket_name
    )

    print(f"[pdf-compressor] fetching R2 object: {object_key}")
    try:
        file_response = await js_fetch(
            url,
            to_js({"method": "GET", "headers": headers}, dict_converter=Object.fromEntries),
        )
    except Exception as exc:
        print(f"[pdf-compressor] fetch exception: {exc}")
        return _error(502, f"Failed to fetch file: {exc}", origin)

    print(f"[pdf-compressor] R2 response status: {file_response.status}")
    if not file_response.ok:
        body_text = await file_response.text()
        print(f"[pdf-compressor] R2 error body: {body_text[:200]}")
        return _error(502, f"R2 returned {file_response.status}: {body_text[:200]}", origin)

    # Stream bytes back to caller with the renamed filename
    array_buffer = await file_response.arrayBuffer()
    resp = Response.new(array_buffer, {"status": 200})
    resp.headers.set("Content-Type", "application/pdf")
    resp.headers.set(
        "Content-Disposition",
        f'attachment; filename="{compressed_filename}"',
    )
    if origin:
        _set_cors_headers(resp, origin)
    return resp


# ---------------------------------------------------------------------------
# Cloudflare Workers entry point
# ---------------------------------------------------------------------------
class Default(WorkerEntrypoint):
    async def on_fetch(self, request):
        env = self.env
        method = request.method.upper()

        # CORS preflight
        if method == "OPTIONS":
            return _handle_preflight(request)

        # Origin check — all non-preflight requests must come from an allowed origin
        origin = request.headers.get("Origin") or ""
        if origin not in ALLOWED_ORIGINS:
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
