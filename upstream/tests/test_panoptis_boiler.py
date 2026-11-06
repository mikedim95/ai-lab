"""Capstone-only boiler regressions using real tools, SQLite files and approvals.

Only model decisions are scripted. Run with network disabled in the capstone
runtime: python /checks/test_panoptis_boiler.py
"""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/app")
sys.path.insert(0, "/checks")
from fake_llm import FakeLLMClient
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, inspect, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.killchain import router
from app.core import database
from app.core.database import Base, get_db
from app.core.dependencies import get_current_admin
from app.labs.killchain import agent, boiler, policy, seed, service
from app.labs.killchain.tools import KillChainGate, ToolContext, build_registry
from app.models import KcBoilerState, User
from app.models.killchain import KcEvent, KcState
from app.services.ollama_client import clear_client_override, set_client_override

TOOL = "set_simulated_boiler_inputs"
INPUTS = {"water_in_pct": 70, "fuel_in_pct": 65}


class PanoptisBoilerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            block = patch(target, side_effect=AssertionError("The capstone simulator must not connect to anything"))
            block.start()
            self.addCleanup(block.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="capstone-boiler-test-")
        self.path = Path(self.directory.name) / "killchain.sqlite3"
        self.open_engine()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.learner = User(username="boiler-owner", email="owner@example.test",
                                password_hash="disabled:test", is_staff=True, is_active=True)
            self.other = User(username="boiler-other", email="other@example.test",
                              password_hash="disabled:test", is_staff=True, is_active=True)
            db.add_all([self.learner, self.other])
            await db.commit()
            await seed.ensure_baseline(db, self.learner.id)
            await seed.ensure_baseline(db, self.other.id)
        self.identity = self.learner

        async def current_admin():
            return self.identity

        async def test_db():
            async with self.sessions() as db:
                yield db
                await db.commit()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_admin] = current_admin
        app.dependency_overrides[get_db] = test_db
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.fake = FakeLLMClient()
        set_client_override(self.fake)
        agent._RUNS.clear()
        agent._BUSY.clear()

    def open_engine(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///" + str(self.path), connect_args={"timeout": 0.25})
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    async def asyncTearDown(self):
        clear_client_override()
        agent._RUNS.clear()
        agent._BUSY.clear()
        await self.client.aclose()
        await self.engine.dispose()
        self.directory.cleanup()

    async def post(self, path, body, expect=200):
        response = await self.client.post("/api/killchain" + path, json=body)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    async def state(self):
        response = await self.client.get("/api/killchain/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def turn(self, arguments=INPUTS):
        self.fake.script_turns([
            {"content": "", "tool_calls": [{"name": TOOL, "arguments": arguments}]},
            {"content": "Simulation updated.", "tool_calls": []},
        ])
        return await self.post("/turn", {"message": "Set inputs on my capstone-only fictional B-01 simulator."})

    async def events(self):
        async with self.sessions() as db:
            rows = (await db.execute(select(KcEvent).where(
                KcEvent.user_id == self.learner.id, KcEvent.kind == boiler.EVENT_KIND,
            ).order_by(KcEvent.id))).scalars()
            return list(rows)

    async def invoke(self, arguments=INPUTS, *, epoch=1, op_id="direct-test"):
        async with self.sessions() as db:
            ctx = ToolContext(db, self.learner.id, op_id, op_id, epoch)
            return await build_registry(ctx).invoke(TOOL, arguments)

    async def test_tool_schema_has_only_two_bounded_inputs(self):
        async with self.sessions() as db:
            registry = build_registry(ToolContext(db, self.learner.id, "schema", "schema", 1))
            tool = registry.get(TOOL)
            self.assertTrue(tool.requires_approval)
            self.assertEqual(len(registry.list_tools()), 9)
            self.assertEqual(tool.parameter_schema["required"], ["water_in_pct", "fuel_in_pct"])
            self.assertFalse(tool.parameter_schema["additionalProperties"])
            self.assertEqual(tool.parameter_schema["properties"], {
                "water_in_pct": {"type": "number", "minimum": 45, "maximum": 75},
                "fuel_in_pct": {"type": "number", "minimum": 45, "maximum": 75},
            })

    async def test_execution_persists_inputs_power_and_matching_before_after_event(self):
        before = (await self.state())["boiler"]
        self.assertEqual((before["water_in_pct"], before["fuel_in_pct"], before["power_output_kw"], before["revision"]),
                         (60, 60, 36, 0))
        result = await self.turn()
        self.assertEqual(result["status"], "completed")
        output = result["steps"][0]["result"]
        self.assertTrue(output["updated"])
        state = await self.state()
        self.assertEqual(output["before"], before)
        self.assertEqual(output["after"], state["boiler"])
        self.assertEqual((output["after"]["water_in_pct"], output["after"]["fuel_in_pct"],
                          output["after"]["power_output_kw"], output["after"]["revision"]), (70, 65, 45.5, 1))
        changed, = await self.events()
        self.assertEqual(changed.id, output["event_id"])
        self.assertEqual(changed.op_id, result["execution_id"])
        self.assertEqual(changed.detail, {"tool": TOOL, "arguments": INPUTS, "before": before,
                                          "after": state["boiler"], "simulated": True, "network": "none"})
        async with self.sessions() as db:
            stored = (await db.execute(select(KcBoilerState).where(KcBoilerState.user_id == self.learner.id))).scalar_one()
            self.assertEqual(boiler.view(stored), state["boiler"])

    async def test_model_claim_without_a_tool_call_changes_nothing(self):
        before = (await self.state())["boiler"]
        self.fake.script_turns([{"content": "Inputs were changed to 75%; power is 56.25 kW.", "tool_calls": []}])
        result = await self.post("/turn", {"message": "Explain the capstone simulator."})
        self.assertEqual(result["steps"], [])
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual(await self.events(), [])

    async def test_bounds_fractional_inputs_and_repeated_inputs_have_deterministic_power(self):
        for revision, (water, fuel, expected) in enumerate([
            (45, 45, 20.25), (75, 75, 56.25), (45, 75, 33.75),
            (60.5, 64.25, 38.87), (60.5, 64.25, 38.87), (60, 60.075, 36.05),
        ], 1):
            with self.subTest(water=water, fuel=fuel):
                result = await self.invoke({"water_in_pct": water, "fuel_in_pct": fuel})
                self.assertTrue(result["updated"])
                self.assertEqual(result["after"]["power_output_kw"], expected)
                self.assertEqual(result["after"]["revision"], revision)
        self.assertEqual(len(await self.events()), 6)

    async def test_invalid_values_are_rejected_by_the_handler_without_state_changes(self):
        before = (await self.state())["boiler"]
        for invalid in (44.999, 75.001, -1, 100, True, False, None, "60", [], {},
                        float("nan"), float("inf"), float("-inf"), 10**500):
            for field in INPUTS:
                with self.subTest(field=field, invalid=repr(invalid)):
                    result = await self.invoke({**INPUTS, field: invalid})
                    self.assertEqual(result["error"], "invalid_arguments")
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual(await self.events(), [])

    async def test_gate_rejects_missing_extra_and_invalid_arguments_without_poisoning_json(self):
        before = (await self.state())["boiler"]
        invalid_calls = [{}, {"water_in_pct": 60}, {**INPUTS, "user_id": self.other.id},
                         {**INPUTS, "expected_revision": 0}, {**INPUTS, "water_in_pct": "60"},
                         {**INPUTS, "fuel_in_pct": True}, {**INPUTS, "fuel_in_pct": float("nan")},
                         {**INPUTS, "fuel_in_pct": float("inf")}, {**INPUTS, "water_in_pct": 76}]
        async with self.sessions() as db:
            ctx = ToolContext(db, self.learner.id, "invalid-gate", "invalid-gate", 1)
            gate = KillChainGate(build_registry(ctx), ctx)
            for arguments in invalid_calls:
                result = await gate.dispatch(TOOL, arguments)
                self.assertEqual(result.observation["error"], "invalid_arguments")
                json.dumps(result.arguments, allow_nan=False)
        state = await self.state()
        json.dumps(state, allow_nan=False)
        self.assertEqual(state["boiler"], before)
        self.assertEqual(state["approvals"], [])
        self.assertEqual(await self.events(), [])

    async def test_invalid_model_arguments_are_rejected_in_every_mode(self):
        before = (await self.state())["boiler"]
        for mode in ("vulnerable", "defended", "guardrailed"):
            await self.post("/mode", {"mode": mode})
            result = await self.turn({"water_in_pct": 80, "fuel_in_pct": 60})
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["steps"][0]["result"]["error"], "invalid_arguments")
            self.assertEqual((await self.state())["boiler"], before)
            self.assertEqual((await self.state())["approvals"], [])
        self.assertEqual(await self.events(), [])

    async def test_medium_approval_executes_once_and_hard_blocks_the_same_call(self):
        for mode in ("defended", "guardrailed"):
            await self.post("/mode", {"mode": mode})
            before = (await self.state())["boiler"]
            paused = await self.turn()
            self.assertEqual(paused["status"], "awaiting_approval")
            self.assertEqual(paused["pending"]["tool"], TOOL)
            self.assertEqual(paused["pending"]["arguments"], INPUTS)
            self.assertEqual((await self.state())["boiler"], before)
            approval_id = paused["pending"]["id"]
            done = await self.post(f"/approvals/{approval_id}/decision", {"decision": "approve"})
            if mode == "defended":
                self.assertEqual(done["approval"]["status"], "executed")
                self.assertEqual(done["approval"]["result"]["before"], before)
                self.assertEqual((await self.state())["boiler"]["revision"], 1)
                changed, = await self.events()
                self.assertEqual(changed.refs["approval_id"], approval_id)
            else:
                self.assertEqual(done["approval"]["status"], "blocked")
                self.assertEqual(done["approval"]["result"]["rail"], "boiler.read_only")
                self.assertIsNone(done["approval"]["executed_at"])
                self.assertEqual((await self.state())["boiler"], before)
            await self.post(f"/approvals/{approval_id}/decision", {"decision": "approve"}, expect=409)
            self.assertEqual(len(await self.events()), 1)

    async def test_rejection_leaves_persistent_simulator_unchanged(self):
        await self.post("/mode", {"mode": "defended"})
        before = (await self.state())["boiler"]
        paused = await self.turn()
        done = await self.post(f"/approvals/{paused['pending']['id']}/decision", {"decision": "reject"})
        self.assertEqual(done["approval"]["status"], "rejected")
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual(await self.events(), [])

    async def test_handler_cannot_skip_approval_change_approved_inputs_or_replay(self):
        await self.post("/mode", {"mode": "defended"})
        before = (await self.state())["boiler"]
        self.assertEqual((await self.invoke())["error"], "approval_required")
        paused = await self.turn()
        pending = paused["pending"]
        token = policy.ApprovalToken(pending["id"], policy.args_hash(TOOL, pending["arguments"]))
        async with self.sessions() as db:
            ctx = ToolContext(db, self.learner.id, "binding", "binding", 1, token)
            changed = await build_registry(ctx).invoke(TOOL, {**INPUTS, "fuel_in_pct": 55})
            self.assertEqual(changed["error"], "approval_required")
        self.assertEqual((await self.state())["boiler"], before)
        await self.post(f"/approvals/{pending['id']}/decision", {"decision": "approve"})
        async with self.sessions() as db:
            ctx = ToolContext(db, self.learner.id, "replay", "replay", 1, token)
            replay = await build_registry(ctx).invoke(TOOL, INPUTS)
            self.assertEqual(replay["error"], "approval_required")
        self.assertEqual(len(await self.events()), 1)

    async def test_each_learner_has_an_independent_simulator(self):
        await self.turn()
        own = (await self.state())["boiler"]
        self.identity = self.other
        other = (await self.state())["boiler"]
        self.assertEqual((other["water_in_pct"], other["fuel_in_pct"], other["revision"]), (60, 60, 0))
        await self.turn({"water_in_pct": 45, "fuel_in_pct": 75})
        self.assertEqual((await self.state())["boiler"]["power_output_kw"], 33.75)
        self.identity = self.learner
        self.assertEqual((await self.state())["boiler"], own)

    async def test_lab1_boiler_in_neighboring_database_is_untouched(self):
        lab1 = Path(self.directory.name) / "nine-labs.sqlite3"
        payload = json.dumps({"labs": {"1": {"boiler": {"water": 12, "fuel": 91, "power": 7.5},
                                                   "boiler_tool_calls": ["Lab 1 sentinel"]}}})
        with sqlite3.connect(lab1) as db:
            db.execute("CREATE TABLE sessions (sid TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("same-browser", payload, 123.0))
        original = lab1.read_bytes()
        await self.turn()
        self.assertEqual(lab1.read_bytes(), original)
        with sqlite3.connect(lab1) as db:
            self.assertEqual(db.execute("SELECT data FROM sessions WHERE sid='same-browser'").fetchone()[0], payload)

    async def test_state_and_audit_survive_reopening_database_and_losing_agent_runtime(self):
        await self.turn()
        before = (await self.state())["boiler"]
        event_ids = [row.id for row in await self.events()]
        agent._RUNS.clear()
        await self.engine.dispose()
        self.open_engine()
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual([row.id for row in await self.events()], event_ids)

    async def test_concurrent_writers_use_revision_comparison_without_lost_updates(self):
        await self.state()
        original = boiler.get_state
        ready = asyncio.Event()
        arrivals = 0

        async def same_revision(db, user_id):
            nonlocal arrivals
            row = await original(db, user_id)
            arrivals += 1
            if arrivals == 2:
                ready.set()
            await ready.wait()
            return row

        async def writer(arguments, op_id):
            return await self.invoke(arguments, op_id=op_id)

        with patch.object(boiler, "get_state", same_revision):
            results = await asyncio.wait_for(asyncio.gather(
                writer({"water_in_pct": 50, "fuel_in_pct": 55}, "writer-a"),
                writer({"water_in_pct": 70, "fuel_in_pct": 75}, "writer-b"),
            ), timeout=10)
        success, = [result for result in results if result.get("updated")]
        conflict, = [result for result in results if "error" in result]
        self.assertEqual(conflict["error"], "state_conflict")
        self.assertEqual((await self.state())["boiler"], success["after"])
        self.assertEqual(len(await self.events()), 1)
        retried = await self.invoke(INPUTS)
        self.assertEqual(retried["before"], success["after"])
        self.assertEqual(retried["after"]["revision"], 2)

    async def test_concurrent_first_reads_create_one_simulator(self):
        async def first_read():
            async with self.sessions() as db:
                row = await boiler.get_state(db, self.learner.id)
                return row.id

        ids = await asyncio.gather(first_read(), first_read())
        self.assertEqual(ids[0], ids[1])
        async with self.sessions() as db:
            rows = (await db.execute(select(KcBoilerState).where(KcBoilerState.user_id == self.learner.id))).scalars().all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].revision, 0)

    async def test_locked_database_is_a_conflict_and_does_not_claim_success(self):
        before = (await self.state())["boiler"]
        lock = sqlite3.connect(self.path)
        try:
            lock.execute("BEGIN IMMEDIATE")
            result = await self.invoke()
            self.assertEqual(result["error"], "state_conflict")
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual(await self.events(), [])

    async def test_failed_audit_write_rolls_back_the_inputs_and_power_together(self):
        before = (await self.state())["boiler"]
        with patch.object(boiler, "emit", AsyncMock(side_effect=SQLAlchemyError("Simulated audit failure"))):
            result = await self.invoke()
        self.assertEqual(result["error"], "state_update_failed")
        self.assertEqual((await self.state())["boiler"], before)
        self.assertEqual(await self.events(), [])

    async def test_soft_reset_preserves_simulator_and_hard_reset_rebuilds_only_owner(self):
        await self.turn()
        changed = (await self.state())["boiler"]
        await self.post("/cleanup/soft_reset", {})
        self.assertEqual((await self.state())["boiler"], changed)
        self.identity = self.other
        await self.turn({"water_in_pct": 50, "fuel_in_pct": 50})
        other = (await self.state())["boiler"]
        self.identity = self.learner
        await self.post("/cleanup/hard_reset", {})
        reset = (await self.state())["boiler"]
        self.assertEqual((reset["water_in_pct"], reset["fuel_in_pct"], reset["power_output_kw"], reset["revision"]),
                         (60, 60, 36, 0))
        self.assertEqual(await self.events(), [])
        self.identity = self.other
        self.assertEqual((await self.state())["boiler"], other)

    async def test_reset_between_read_and_update_prevents_stale_write(self):
        await self.state()
        original = boiler.get_state

        async def reset_after_read(db, user_id):
            row = await original(db, user_id)
            async with self.sessions() as other_db:
                await service.hard_reset(other_db, user_id)
            return row

        with patch.object(boiler, "get_state", reset_after_read):
            result = await self.invoke()
        self.assertEqual(result["error"], "state_conflict")
        self.assertEqual((await self.state())["boiler"]["revision"], 0)
        self.assertEqual(await self.events(), [])
        stale = await self.invoke(epoch=1)
        self.assertIn("reset", stale["error"])
        self.assertEqual(await self.events(), [])

    async def test_database_constraints_reject_out_of_range_writes(self):
        before = (await self.state())["boiler"]
        for field in ("water_in_pct", "fuel_in_pct"):
            async with self.sessions() as db:
                with self.assertRaises(IntegrityError):
                    await db.execute(update(KcBoilerState).where(KcBoilerState.user_id == self.learner.id).values(**{field: 80}))
                await db.rollback()
        self.assertEqual((await self.state())["boiler"], before)

    async def test_additive_bootstrap_preserves_existing_schema_and_learner_state(self):
        legacy = create_async_engine("sqlite+aiosqlite:///" + str(Path(self.directory.name) / "legacy.sqlite3"))
        try:
            tables = [table for table in Base.metadata.sorted_tables if table.name != KcBoilerState.__tablename__]
            async with legacy.begin() as connection:
                await connection.run_sync(lambda db: Base.metadata.create_all(db, tables=tables))
            legacy_sessions = async_sessionmaker(legacy, expire_on_commit=False)
            async with legacy_sessions() as db:
                db.add(User(id=1, username="existing", email="existing@example.test", password_hash="disabled:test"))
                db.add(KcState(user_id=1, mode="defended", epoch=9,
                               conversation=[{"role": "user", "content": "Preserve existing learner state"}]))
                await db.commit()
            async with legacy.connect() as connection:
                before = set(await connection.run_sync(lambda db: inspect(db).get_table_names()))
            with patch.object(database, "engine", legacy):
                await database.init_db()
            async with legacy.connect() as connection:
                after = set(await connection.run_sync(lambda db: inspect(db).get_table_names()))
            self.assertEqual(after - before, {"kc_boiler_state"})
            async with legacy_sessions() as db:
                state = (await db.execute(select(KcState).where(KcState.user_id == 1))).scalar_one()
                self.assertEqual((state.mode, state.epoch, state.conversation),
                                 ("defended", 9, [{"role": "user", "content": "Preserve existing learner state"}]))
                simulator = await boiler.get_state(db, 1)
                self.assertEqual((simulator.water_in_pct, simulator.fuel_in_pct, simulator.revision), (60, 60, 0))
        finally:
            await legacy.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)
