"""Postgres migration POC tests. Skipped entirely unless AGENT_PG_TEST_DSN
points at a reachable Postgres — this must never block `make test`, and does
not run there. See wiki/postgres-migration-plan.md for what this does and
does not prove."""
import asyncio
import os
import time
import unittest
import uuid

try:
    import asyncpg
    from agents.action_runtime_pg_poc import PgActionRuntimePOC
except ImportError:
    asyncpg = None
    PgActionRuntimePOC = None

DSN = os.getenv("AGENT_PG_TEST_DSN", "")


def _skip_reason():
    if asyncpg is None:
        return "asyncpg not installed (optional POC dependency)"
    if not DSN:
        return "AGENT_PG_TEST_DSN not set; Postgres migration POC is opt-in, see make pg-poc-up/pg-poc-test"
    return None


@unittest.skipIf(_skip_reason(), _skip_reason() or "")
class PostgresPocTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.runtime = await asyncio.wait_for(PgActionRuntimePOC.connect(DSN), 10)
        except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as ex:
            raise unittest.SkipTest(f"Postgres unreachable at AGENT_PG_TEST_DSN: {type(ex).__name__}")
        await self.runtime.reset()

    async def asyncTearDown(self):
        await self.runtime.close()

    async def test_concurrent_same_idempotency_key_lands_exactly_once(self):
        task_id = await self.runtime.create_task("alice")
        results = await asyncio.gather(*[
            self.runtime.attempt_action(task_id, "refund:" + str(task_id), "request_refund")
            for _ in range(10)
        ])
        self.assertEqual(sum(1 for r in results if r), 1, "exactly one concurrent attempt should win the insert")
        self.assertEqual(await self.runtime.count_actions(task_id), 1)

    async def test_distinct_idempotency_keys_on_same_task_all_land(self):
        task_id = await self.runtime.create_task("alice")
        results = await asyncio.gather(*[
            self.runtime.attempt_action(task_id, f"step-{i}:{task_id}", "query_billing")
            for i in range(5)
        ])
        self.assertTrue(all(results))
        self.assertEqual(await self.runtime.count_actions(task_id), 5)

    async def test_exception_before_commit_leaves_no_row(self):
        task_id = await self.runtime.create_task("alice")
        with self.assertRaises(RuntimeError):
            await self.runtime.attempt_action(task_id, "will-rollback", "sync_entitlements", fail_before_commit=True)
        self.assertEqual(await self.runtime.count_actions(task_id), 0)

    async def test_advisory_lock_serializes_same_task_not_parallelizes(self):
        # Two transactions on the SAME task_id must not hold the advisory lock
        # at the same time: prove it by timing a held lock against a second
        # acquirer, rather than trusting that a lock object "should" work.
        task_id = str(await self.runtime.create_task("alice"))
        started_second = asyncio.Event()
        timeline = {}

        async def hold_lock(label, hold_seconds):
            async with self.runtime.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", task_id)
                    timeline[label + "_acquired"] = time.monotonic()
                    if label == "second":
                        started_second.set()
                    await asyncio.sleep(hold_seconds)
                    timeline[label + "_released"] = time.monotonic()

        first = asyncio.create_task(hold_lock("first", 0.4))
        await asyncio.sleep(0.05)  # let "first" acquire before "second" starts waiting
        second = asyncio.create_task(hold_lock("second", 0.05))
        await asyncio.gather(first, second)

        self.assertLessEqual(
            timeline["first_released"], timeline["second_acquired"] + 0.02,
            "second acquirer must not enter the critical section before the first releases the lock",
        )

    async def test_unrelated_tasks_do_not_block_each_other(self):
        # Different task_ids hash to (almost certainly) different advisory
        # lock keys, so this should run essentially in parallel, not
        # serialize like the same-task case above.
        task_a, task_b = str(await self.runtime.create_task("alice")), str(await self.runtime.create_task("bob"))

        async def hold_lock(task_id, hold_seconds):
            async with self.runtime.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1::text))", task_id)
                    await asyncio.sleep(hold_seconds)

        started = time.monotonic()
        await asyncio.gather(hold_lock(task_a, 0.3), hold_lock(task_b, 0.3))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.55, "unrelated tasks serialized when they should have run concurrently")


if __name__ == "__main__":
    unittest.main()
