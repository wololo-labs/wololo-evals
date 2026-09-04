"""Test harness: a real throwaway PostgreSQL cluster from local binaries.

No Docker daemon required (and none exists in the reference environment):
``initdb`` + ``postgres`` are found on PATH or under ``/usr/lib/postgresql/*``.
Unix-socket, trust-auth, session-scoped; the schema is applied once and each
test runs against real RLS with the real roles (`wololo_evals_app`,
`wololo_evals_worker`) — tenancy tests that "pass" against a mock RLS layer
prove nothing.

The data directory lives under pytest's ``tmp_path_factory``, but the unix
socket directory deliberately does not: ``sockaddr_un.sun_path`` is 104 bytes
on macOS and 108 on Linux, and the default macOS ``TMPDIR``
(``/var/folders/<hash>/<hash>/T/``) already spends ~50 of them, so
``<tmp>/pytest-of-<user>/pytest-<n>/pg0/sock/.s.PGSQL.<port>`` overflows and
``postgres`` exits before it can listen. :func:`_socket_dir` puts the socket
under a short root instead, so a plain ``pytest -q`` works on both platforms
with no ``TMPDIR`` workaround.
"""
from __future__ import annotations

import getpass
import os
import pathlib
import shutil
import socket
import subprocess
import tempfile
import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "src" / "wololo_evals" / "migrations"


def _find_pg_bin() -> pathlib.Path | None:
    for name in ("initdb",):
        found = shutil.which(name)
        if found:
            return pathlib.Path(found).parent
    candidates = sorted(pathlib.Path("/usr/lib/postgresql").glob("*/bin"), reverse=True)
    for c in candidates:
        if (c / "initdb").exists():
            return c
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: Longest ``sun_path`` any supported platform accepts, minus the
#: ``/.s.PGSQL.<5-digit port>`` postgres appends to the directory we hand it.
_MAX_SOCKET_DIR_LEN = 104 - len("/.s.PGSQL.65535") - 1


def _socket_dir() -> pathlib.Path:
    """A short-enough directory for the postgres unix socket.

    Honours ``WOLOLO_EVALS_PG_SOCKET_ROOT`` when set (CI/sandboxes that forbid
    ``/tmp``), else the shortest of ``/tmp`` and the platform temp dir. Fails
    loudly rather than handing postgres a path it will silently truncate.
    """
    roots = [os.environ.get("WOLOLO_EVALS_PG_SOCKET_ROOT"), "/tmp", tempfile.gettempdir()]
    for root in roots:
        if not root:
            continue
        base = pathlib.Path(root)
        if not base.is_dir():
            continue
        candidate = pathlib.Path(tempfile.mkdtemp(prefix="we-pg-", dir=str(base)))
        if len(str(candidate)) <= _MAX_SOCKET_DIR_LEN:
            return candidate
        shutil.rmtree(candidate, ignore_errors=True)
    raise RuntimeError(
        f"no temp root yields a unix socket path under {_MAX_SOCKET_DIR_LEN} chars "
        f"(tried {roots}); set WOLOLO_EVALS_PG_SOCKET_ROOT to a short directory"
    )


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    pg_bin = _find_pg_bin()
    if pg_bin is None:
        pytest.fail(
            "PostgreSQL binaries not found (PATH or /usr/lib/postgresql/*/bin). "
            "The invariant suites REQUIRE a real Postgres; install one. "
            "(Deliberately a failure, not a skip: a silently-skipped tenancy "
            "suite would fake invariant coverage.)"
        )
    base = tmp_path_factory.mktemp("pg")
    datadir = base / "data"
    sockdir = _socket_dir()
    port = _free_port()
    superuser = getpass.getuser()

    pg_env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    subprocess.run(
        [
            str(pg_bin / "initdb"),
            "-D", str(datadir),
            "-A", "trust",
            "-U", superuser,
            "--no-locale",
            "-E", "UTF8",
        ],
        check=True,
        capture_output=True,
        env=pg_env,
    )
    proc = subprocess.Popen(
        [
            str(pg_bin / "postgres"),
            "-D", str(datadir),
            "-k", str(sockdir),
            "-p", str(port),
            "-c", "listen_addresses=",
            "-c", "fsync=off",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=pg_env,
    )
    # wait for readiness
    for _ in range(100):
        ready = subprocess.run(
            [str(pg_bin / "pg_isready"), "-h", str(sockdir), "-p", str(port)],
            capture_output=True,
        )
        if ready.returncode == 0:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("postgres did not become ready")

    subprocess.run(
        [str(pg_bin / "createdb"), "-h", str(sockdir), "-p", str(port), "wololo_evals"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            str(pg_bin.parent.parent / "bin" / "psql")
            if (pg_bin.parent.parent / "bin" / "psql").exists()
            else shutil.which("psql") or str(pg_bin / "psql"),
            "-h", str(sockdir), "-p", str(port),
            "-d", "wololo_evals",
            "-v", "ON_ERROR_STOP=1",
            "-q",
            *[arg for m in sorted(MIGRATIONS_DIR.glob("*.sql")) for arg in ("-f", str(m))],
        ],
        check=True,
        capture_output=True,
    )

    def url(user: str) -> str:
        return (
            f"postgresql+psycopg://{user}@/wololo_evals"
            f"?host={sockdir}&port={port}"
        )

    info = {
        "owner_url": url(superuser),
        "app_url": url("wololo_evals_app"),
        "worker_url": url("wololo_evals_worker"),
        "sockdir": str(sockdir),
        "port": port,
    }
    yield info
    # SIGINT = postgres fast shutdown: disconnects lingering test clients
    # instead of waiting on them (SIGTERM smart shutdown hangs the teardown).
    import signal

    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(sockdir, ignore_errors=True)


@pytest.fixture(scope="session")
async def owner_engine(pg):
    eng = create_async_engine(pg["owner_url"], connect_args={"options": "-c timezone=UTC"})
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
async def app_engine(pg):
    eng = create_async_engine(pg["app_url"], connect_args={"options": "-c timezone=UTC"})
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
async def worker_engine(pg):
    eng = create_async_engine(pg["worker_url"], connect_args={"options": "-c timezone=UTC"})
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session", autouse=True)
async def seed_tenants(pg, owner_engine):
    """The two standing proof tenants (DOCTRINE Section 5): Meridian and its
    neighbour Northwind, whose whole job is to try to cross the fence."""
    async with owner_engine.begin() as conn:
        for tid, name in (("meridian", "Meridian Financial"), ("northwind", "Northwind")):
            await conn.execute(
                text(
                    "insert into wololo_evals.tenants (tenant_id, name) "
                    "values (:t, :n) on conflict (tenant_id) do nothing"
                ),
                {"t": tid, "n": name},
            )


@pytest.fixture()
def master_key() -> str:
    return "aa" * 32


@pytest.fixture()
def cipher(master_key, tmp_path):
    from wololo_evals.crypto import EnvelopeCipher

    return EnvelopeCipher(master_key, "test-master-1", str(tmp_path / "keyring.json"))


@pytest.fixture()
def blobstore(tmp_path):
    from wololo_evals.blobstore import FsBlobStore

    return FsBlobStore(str(tmp_path / "blobs"))
