"""A tiny parcel-quoting HTTP service on the standard library only.

``POST /quotes`` prices a parcel, ``GET /health`` reports liveness. No
framework, no database, no network calls: the whole fixture starts in-process
in milliseconds, which is what makes it usable as the code under test for a
mutation run that boots it dozens of times.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from quoting.pricing import QuoteRequestError, parse_quote_request, quote_pence

__all__ = ["QuoteHandler", "build_server", "handle_quote"]

MAX_BODY_BYTES = 64 * 1024


def handle_quote(body: bytes) -> tuple[int, dict]:
    """Price a request body. Returns ``(http status, response payload)``.

    Split out from the handler so the pricing path is exercisable without a
    socket; the handler below is a thin adapter over it.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"error": "body is not valid JSON", "field": "body"}

    try:
        request = parse_quote_request(payload)
    except QuoteRequestError as exc:
        return 400, {"error": exc.message, "field": exc.field}

    return 200, {
        "quote_pence": quote_pence(request),
        "weight_grams": request.weight_grams,
        "destination": request.destination,
        "express": request.express,
    }


class QuoteHandler(BaseHTTPRequestHandler):
    """Routes ``GET /health`` and ``POST /quotes``; everything else is 404."""

    server_version = "quoting-fixture/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default stderr access log: a mutation run starts this
        server many times and the noise buries the real output."""

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/quotes":
            self._respond(404, {"error": "not found", "path": self.path})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": "request body too large", "field": "body"})
            return
        status, payload = handle_quote(self.rfile.read(length))
        self._respond(status, payload)


def build_server(port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Bind a server without serving it. Port ``0`` picks a free port, so
    parallel test runs never collide."""
    return ThreadingHTTPServer((host, port), QuoteHandler)


def main(port: int = 8080) -> None:  # pragma: no cover - manual entry point
    server = build_server(port)
    print(f"quoting fixture listening on http://{server.server_address[0]}:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover - manual entry point
    main()
