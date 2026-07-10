"""Async engine + the tenant-scoped transaction every handler runs inside.

SPEC S4.1: ``set_config(..., true)`` is transaction-local, so a pooled
connection can never leak one tenant's scope into the next transaction. The
FastAPI dependency chain is: validate_entitlement_token (vendored identity
family) -> resolved tenant_id + actor + features -> ``tenant_txn(tenant_id)``.
The handler never receives a raw request-body tenant_id; the token claim is
the only source.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    # timezone=UTC so timestamptz round-trips match the audit-chain hashing
    # (audit.py canonicalizes timestamps as UTC isoformat strings).
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


@asynccontextmanager
async def tenant_txn(engine: AsyncEngine, tenant_id: str):
    """One transaction scoped to *tenant_id* via the app.tenant_id GUC.

    Transaction-local (`set_config(..., true)`): commit/rollback clears it.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for a tenant transaction")
    async with engine.begin() as conn:
        await conn.execute(
            text("select set_config('app.tenant_id', :t, true)"), {"t": tenant_id}
        )
        yield conn
