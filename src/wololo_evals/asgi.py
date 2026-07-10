"""Production ASGI entrypoint: ``uvicorn wololo_evals.asgi:app``.

Builds the app's dependencies from the environment (``settings()``), installs
OTEL tracing (OTLP when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, else in-process),
and hands them to :func:`create_app`. Everything security-load-bearing has no
silent default (see ``settings.py``); this module only wires them together.
"""
from __future__ import annotations

from .api import AppDeps, create_app
from .auth import Verifier
from .blobstore import make_blobstore
from .crypto import EnvelopeCipher
from .db import make_engine
from .otel import init_tracing
from .settings import settings


def build_app():
    cfg = settings()
    init_tracing()
    deps = AppDeps(
        engine=make_engine(cfg.database_url),
        verifier=Verifier(cfg.jwks_path, cfg.expected_iss),
        cipher=EnvelopeCipher(cfg.master_key_hex, cfg.master_key_id, cfg.keyring_path),
        blobstore=make_blobstore(cfg),
    )
    return create_app(deps)


app = build_app()
