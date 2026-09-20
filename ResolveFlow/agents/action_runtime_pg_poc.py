"""Postgres migration POC — proves three specific properties on a real
database, not a port of ActionRuntime's business logic.

See wiki/postgres-migration-plan.md. This file only implements: creating a
task row, taking a per-task advisory lock for mutual exclusion (the Postgres
equivalent of the SQLite runtime's lease table), and inserting an action
guarded by a real UNIQUE constraint on the idempotency key (instead of
scanning a JSON array for a matching key). GoalInterpreter, plans, approvals,
confirmations and every other production concern stay in agents/action_runtime.py.
"""
import uuid

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS poc_tasks (
    id UUID PRIMARY KEY,
    owner TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poc_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES poc_tasks(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    tool TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PgActionRuntimePOC:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn, *, min_size=1, max_size=10):
        pool = await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
        instance = cls(pool)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return instance

    async def close(self):
        await self.pool.close()

    async def reset(self):
        """Test-only: wipe POC tables between test cases."""
        async with self.pool.acquire() as conn:
            await conn.execute("TRUNCATE poc_actions, poc_tasks")

    async def create_task(self, owner):
        task_id = uuid.uuid4()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO poc_tasks(id, owner, status) VALUES ($1, $2, 'running')", task_id, owner
            )
        return task_id

    async def attempt_action(self, task_id, idempotency_key, tool, *, fail_before_commit=False):
        """Acquire a per-task advisory lock for one transaction, then attempt
        an idempotent insert. Two concurrent callers for the SAME task_id
        serialize on the advisory lock (the second only proceeds once the
        first's transaction ends) rather than both executing at once — this
        is the Postgres equivalent of the SQLite runtime's lease table, but
        released automatically at transaction end instead of by a polled
        expiry. Returns True only if THIS call's insert was the one that
        actually landed (first writer wins); a duplicate idempotency_key from
        a later attempt returns False without raising.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", str(task_id))
                row = await conn.fetchrow(
                    "INSERT INTO poc_actions(task_id, idempotency_key, tool, status) "
                    "VALUES ($1, $2, $3, 'done') ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
                    task_id, idempotency_key, tool,
                )
                if fail_before_commit:
                    raise RuntimeError("Simulated failure before commit")
                return row is not None

    async def count_actions(self, task_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM poc_actions WHERE task_id=$1", task_id)
