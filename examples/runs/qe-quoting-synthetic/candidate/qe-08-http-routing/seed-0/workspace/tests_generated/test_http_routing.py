"""Routing and the health endpoint, driven over a real socket: the handler's
status codes and headers are part of the contract, and handle_quote alone
cannot see them.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from quoting.service import build_server


@pytest.fixture()
def base_url():
    server = build_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_health_reports_ok(base_url):
    with urllib.request.urlopen(f"{base_url}/health", timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())["status"] == "ok"


def test_quotes_prices_over_http(base_url):
    request = urllib.request.Request(
        f"{base_url}/quotes",
        data=json.dumps({"weight_grams": 1000, "destination": "uk"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "application/json"
        assert json.loads(response.read())["quote_pence"] == 515


def test_an_unknown_path_is_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{base_url}/nope", timeout=5)
    assert excinfo.value.code == 404
    assert json.loads(excinfo.value.read())["path"] == "/nope"


def test_posting_to_an_unknown_path_is_404(base_url):
    request = urllib.request.Request(
        f"{base_url}/quote", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 404
