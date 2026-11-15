"""Durable scope transactions. Schema installation is an explicit operator action."""
from contextlib import asynccontextmanager
from .contract import AccountingError

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounting_scope_state (
 tenant_id text NOT NULL, subject_id text NOT NULL, state jsonb NOT NULL,
 PRIMARY KEY (tenant_id, subject_id)
)
"""


class PostgresRepository:
    """Serialize a scope's quotas, references and replay results in one transaction.

    A scope lock is stronger than separate unit locks: it also protects replay keys
    reused across operations/units. Independent scopes can proceed concurrently.
    """
    def __init__(self, dsn):
        if not isinstance(dsn, str) or not dsn.startswith(('postgresql://', 'postgres://')):
            raise AccountingError('invalid_request')
        self._dsn = dsn

    async def initialize(self):
        import psycopg
        async with await psycopg.AsyncConnection.connect(self._dsn, connect_timeout=2) as conn:
            await conn.execute(SCHEMA)

    @asynccontextmanager
    async def transaction(self, scope):
        import psycopg
        from psycopg.types.json import Jsonb
        try:
            async with await psycopg.AsyncConnection.connect(self._dsn, connect_timeout=2) as conn:
                await conn.execute("SET LOCAL statement_timeout = '10s'")
                await conn.execute("SET LOCAL lock_timeout = '5s'")
                key = (scope.tenant_id, scope.subject_id)
                await conn.execute('INSERT INTO accounting_scope_state VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (*key, Jsonb({})))
                cursor = await conn.execute('SELECT state FROM accounting_scope_state WHERE tenant_id=%s AND subject_id=%s FOR UPDATE', key)
                state = (await cursor.fetchone())[0]
                yield state
                await conn.execute('UPDATE accounting_scope_state SET state=%s WHERE tenant_id=%s AND subject_id=%s', (Jsonb(state), *key))
        except psycopg.Error:
            raise AccountingError('unavailable') from None
