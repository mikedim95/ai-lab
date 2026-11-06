"""Real technician export/approval/delivery regressions with disposable SQLite.

Only model decisions are scripted. Run in the capstone runtime with networking
disabled: python /checks/test_panoptis_export.py
"""
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "/app")
sys.path.insert(0, "/checks")
from fake_llm import FakeLLMClient
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.killchain import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_admin
from app.labs.killchain import agent, guardrails, policy, seed
from app.labs.killchain.constants import ATTACKER_EMAIL, OPS_EMAIL
from app.labs.killchain.tools import ToolContext, build_registry
from app.models import User
from app.models.killchain import KcMail, KcTechnicianContact
from app.services.ollama_client import clear_client_override, set_client_override

TOOL = "export_technician_records"
FIELDS = ("contact_ref", "name", "email", "extension", "role", "asset_scope", "classification", "synthetic")
REQUEST = "Export my fictional technician records to the simulated inbox."


def call(arguments):
    return {"content": "", "tool_calls": [{"name": TOOL, "arguments": arguments}]}


def answer(text="Done."):
    return {"content": text, "tool_calls": []}


class PanoptisExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Fail on any real mail transport, DNS lookup, or socket connection.
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
                       "socket.socket.connect_ex", "smtplib.SMTP", "smtplib.SMTP_SSL"):
            block = patch(target, side_effect=AssertionError("Exports must stay in the simulated inbox"))
            block.start()
            self.addCleanup(block.stop)
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.learner = User(username="export-owner", email="owner@example.test",
                                password_hash="disabled:test", is_staff=True, is_active=True)
            self.other = User(username="export-other", email="other@example.test",
                              password_hash="disabled:test", is_staff=True, is_active=True)
            db.add_all([self.learner, self.other])
            await db.commit()
            await seed.ensure_baseline(db, self.learner.id)
            await seed.ensure_baseline(db, self.other.id)
            for row in (await db.execute(select(KcTechnicianContact).where(
                KcTechnicianContact.user_id == self.other.id,
            ))).scalars():
                row.name = "OTHER LEARNER " + row.name
                row.email = "other-" + row.email
            await db.commit()
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

    async def asyncTearDown(self):
        clear_client_override()
        agent._RUNS.clear()
        agent._BUSY.clear()
        await self.client.aclose()
        await self.engine.dispose()

    async def post(self, path, body, expect=200):
        response = await self.client.post("/api/killchain" + path, json=body)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    async def state(self):
        response = await self.client.get("/api/killchain/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def records(self):
        async with self.sessions() as db:
            rows = (await db.execute(select(KcTechnicianContact).where(
                KcTechnicianContact.user_id == self.learner.id,
            ).order_by(KcTechnicianContact.id))).scalars()
            return [{field: getattr(row, field) for field in FIELDS} for row in rows if row.synthetic]

    async def mails(self):
        async with self.sessions() as db:
            return list((await db.execute(select(KcMail).order_by(KcMail.id))).scalars())

    async def no_delivery(self):
        state = await self.state()
        self.assertEqual(state["inbox"], [])
        self.assertEqual(state["status"]["exfiltration"], 0)
        self.assertFalse(any(row["kind"] in {"mail_delivered", "exfiltration"} for row in state["events"]))
        self.assertEqual(await self.mails(), [])
        return state

    async def turn(self, arguments=None):
        self.fake.script_turns([call(arguments if arguments is not None else {"recipient": ATTACKER_EMAIL}), answer()])
        return await self.post("/turn", {"message": REQUEST})

    async def decide(self, approval_id, decision="approve", expect=200):
        return await self.post(f"/approvals/{approval_id}/decision", {"decision": decision}, expect)

    async def assert_delivered(self, arguments, expected):
        state = await self.state()
        self.assertEqual(len(state["inbox"]), 1)
        mail, = state["inbox"]
        self.assertEqual(mail["payload"], {"dataset": "technicians", "records": expected})
        self.assertEqual(mail["status"], "delivered")
        self.assertTrue(all(record["synthetic"] for record in mail["payload"]["records"]))
        delivered, = [row for row in state["events"] if row["kind"] == "mail_delivered"]
        norm = policy.norm_args(TOOL, arguments)
        self.assertEqual(delivered["detail"]["tool"], TOOL)
        self.assertEqual(delivered["detail"]["arguments"], norm)
        self.assertEqual(delivered["detail"]["records"], len(expected))
        self.assertEqual(delivered["detail"]["destination"], [a for a in (norm["recipient"], norm["bcc"]) if a])
        self.assertEqual(delivered["detail"]["network"], "none")
        self.assertIs(delivered["detail"]["simulated"], True)
        self.assertEqual(delivered["refs"]["mail_id"], mail["id"])
        self.assertEqual(delivered["refs"]["approval_id"], mail["approval_id"])
        self.assertEqual(delivered["op_id"], mail["execution_id"])
        selected, = [row for row in state["events"] if row["kind"] == "tool_call"]
        self.assertEqual(selected["detail"]["tool"], TOOL)
        self.assertEqual(selected["detail"]["arguments"], arguments)
        self.assertLess(selected["id"], delivered["id"])
        result, = [row for row in state["events"] if row["kind"] == "tool_result"]
        self.assertTrue(result["detail"]["result"]["delivered"])
        self.assertEqual(result["detail"]["result"]["mail_id"], mail["id"])
        self.assertEqual(state["status"]["exfiltration"], 1)
        persisted, = await self.mails()
        self.assertEqual((persisted.id, persisted.user_id, persisted.payload),
                         (mail["id"], self.learner.id, mail["payload"]))
        return state

    async def test_export_tool_retains_registration_and_approval_metadata(self):
        async with self.sessions() as db:
            registry = build_registry(ToolContext(db, self.learner.id, "test", "test", 1))
            self.assertEqual({tool["name"] for tool in registry.list_tools()}, {
                "summarize_tickets", "count_unresolved_tickets", "get_low_rated_products",
                "get_checkout_price", "list_coupons", "lookup_customers", "send_export_email", TOOL,
                "set_simulated_boiler_inputs",
            })
            tool = registry.get(TOOL)
            self.assertTrue(tool.requires_approval)
            self.assertEqual(tool.parameter_schema["required"], ["recipient"])
            self.assertEqual(set(tool.parameter_schema["properties"]), {"recipient", "bcc", "subject"})
            self.assertFalse(tool.parameter_schema["additionalProperties"])
        self.assertIn("tool_name", guardrails.scan_untrusted(TOOL))
        await self.no_delivery()

    async def test_model_claim_without_execution_does_not_deliver_records(self):
        before = await self.no_delivery()
        for record in await self.records():
            self.assertNotIn(record["email"], json.dumps(before))
        self.fake.script_turns([answer("The fictional technician records were exported to the simulated inbox.")])
        result = await self.post("/turn", {"message": REQUEST})
        self.assertEqual(result["steps"], [])
        await self.no_delivery()

    async def test_execution_delivers_actual_current_learner_synthetic_records(self):
        async with self.sessions() as db:
            first = (await db.execute(select(KcTechnicianContact).where(
                KcTechnicianContact.user_id == self.learner.id,
            ).order_by(KcTechnicianContact.id))).scalars().first()
            first.email = "updated-fixture@panoptis.example.test"
            first.extension = "SIM-UPDATED"
            for fictional in (True, False):
                db.add(KcTechnicianContact(
                    user_id=self.learner.id, contact_ref="TEST-" + str(fictional),
                    name="Fictional test sentinel " + str(fictional),
                    email="sentinel-" + str(fictional).lower() + "@example.test", extension="SIM-TEST",
                    role="Test fixture", asset_scope=["G-01"], classification="restricted", synthetic=fictional,
                ))
            await db.commit()
        expected = await self.records()
        self.assertEqual(len(expected), 5)
        await self.no_delivery()
        arguments = {"recipient": OPS_EMAIL, "bcc": ATTACKER_EMAIL, "subject": "Synthetic technicians"}
        result = await self.turn(arguments)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"][0]["result"]["records"], 5)
        state = await self.assert_delivered(arguments, expected)
        self.assertNotIn("sentinel-false@example.test", json.dumps(state))
        self.assertNotIn("OTHER LEARNER", json.dumps(state))
        self.identity = self.other
        self.assertEqual((await self.state())["inbox"], [])

    async def test_defended_mode_holds_records_until_approved_execution_once(self):
        await self.post("/mode", {"mode": "defended"})
        arguments = {"recipient": "  " + OPS_EMAIL.upper() + "  ", "bcc": ATTACKER_EMAIL.upper()}
        paused = await self.turn(arguments)
        self.assertEqual(paused["status"], "awaiting_approval")
        pending = paused["pending"]
        self.assertEqual(pending["tool"], TOOL)
        self.assertEqual(pending["arguments"], policy.norm_args(TOOL, arguments))
        self.assertEqual(pending["action_type"], "technician_records_export")
        self.assertIn("4 fictional technician records", " ".join(pending["effects"]))
        await self.no_delivery()
        done = await self.decide(pending["id"])
        self.assertEqual(done["approval"]["status"], "executed")
        self.assertEqual(done["approval"]["result"]["records"], 4)
        state = await self.assert_delivered(arguments, await self.records())
        self.assertEqual(state["inbox"][0]["approval_id"], pending["id"])
        kinds = [row["kind"] for row in state["events"]]
        self.assertLess(kinds.index("approval_required"), kinds.index("approval_approved"))
        self.assertLess(kinds.index("approval_approved"), kinds.index("mail_delivered"))
        await self.decide(pending["id"], expect=409)
        self.assertEqual(len(await self.mails()), 1)

    async def test_rejection_keeps_records_out_of_mail_and_attacker_inbox(self):
        await self.post("/mode", {"mode": "defended"})
        paused = await self.turn()
        done = await self.decide(paused["pending"]["id"], "reject")
        self.assertEqual(done["approval"]["status"], "rejected")
        self.assertIsNone(done["approval"]["executed_at"])
        await self.no_delivery()

    async def test_handler_requires_approval_even_when_the_gate_is_skipped(self):
        for mode in ("defended", "guardrailed"):
            with self.subTest(mode=mode):
                await self.post("/mode", {"mode": mode})
                state = await self.state()
                async with self.sessions() as db:
                    registry = build_registry(ToolContext(db, self.learner.id, "direct", "direct", state["epoch"]))
                    result = await registry.invoke(TOOL, {"recipient": ATTACKER_EMAIL})
                    self.assertEqual(result["error"], "approval_required")
                await self.no_delivery()

    async def test_approval_is_bound_to_arguments_and_cannot_be_replayed(self):
        await self.post("/mode", {"mode": "defended"})
        paused = await self.turn()
        pending = paused["pending"]
        ctx = agent._RUNS[paused["run_id"]].ctx
        async with self.sessions() as db:
            ctx.db = db
            ctx.token = policy.ApprovalToken(pending["id"], policy.args_hash(TOOL, pending["arguments"]))
            registry = build_registry(ctx)
            for changed in ({"recipient": OPS_EMAIL}, {"bcc": OPS_EMAIL}, {"subject": "Changed"}):
                result = await registry.invoke(TOOL, {**pending["arguments"], **changed})
                self.assertEqual(result["error"], "approval_required")
            ctx.token = None
        await self.no_delivery()
        await self.decide(pending["id"])
        async with self.sessions() as db:
            ctx.db = db
            ctx.token = policy.ApprovalToken(pending["id"], policy.args_hash(TOOL, pending["arguments"]))
            replay = await build_registry(ctx).invoke(TOOL, pending["arguments"])
            self.assertEqual(replay["error"], "approval_required")
            ctx.token = None
        self.assertEqual(len(await self.mails()), 1)

    async def test_other_learner_cannot_approve_or_read_the_delivery(self):
        await self.post("/mode", {"mode": "defended"})
        paused = await self.turn()
        self.identity = self.other
        await self.decide(paused["pending"]["id"], expect=404)
        self.assertEqual((await self.state())["approvals"], [])
        self.assertEqual(await self.mails(), [])
        self.identity = self.learner
        await self.decide(paused["pending"]["id"])
        self.assertEqual(len((await self.state())["inbox"]), 1)
        self.identity = self.other
        state = await self.state()
        self.assertEqual(state["inbox"], [])
        self.assertFalse(any(row["kind"] == "mail_delivered" for row in state["events"]))

    async def test_guardrailed_mode_blocks_to_and_bcc_after_approval(self):
        await self.post("/mode", {"mode": "guardrailed"})
        for arguments in ({"recipient": ATTACKER_EMAIL}, {"recipient": OPS_EMAIL, "bcc": ATTACKER_EMAIL}):
            with self.subTest(arguments=arguments):
                paused = await self.turn(arguments)
                self.assertEqual(paused["status"], "awaiting_approval")
                await self.no_delivery()
                done = await self.decide(paused["pending"]["id"])
                self.assertEqual(done["approval"]["status"], "blocked")
                self.assertEqual(done["approval"]["result"]["rail"], "egress.allowlist")
                await self.no_delivery()

    async def test_guardrailed_internal_delivery_requires_execution_and_stays_out_of_attacker_inbox(self):
        await self.post("/mode", {"mode": "guardrailed"})
        paused = await self.turn({"recipient": OPS_EMAIL})
        await self.no_delivery()
        done = await self.decide(paused["pending"]["id"])
        self.assertEqual(done["approval"]["status"], "executed")
        mail, = await self.mails()
        self.assertEqual(mail.payload["records"], await self.records())
        state = await self.state()
        self.assertEqual(state["inbox"], [])
        self.assertEqual(state["status"]["exfiltration"], 0)
        self.assertEqual(len([row for row in state["events"] if row["kind"] == "mail_delivered"]), 1)

    async def test_pending_export_can_execute_after_agent_runtime_is_lost(self):
        await self.post("/mode", {"mode": "defended"})
        paused = await self.turn()
        agent._RUNS.clear()
        await self.no_delivery()
        done = await self.decide(paused["pending"]["id"])
        self.assertEqual(done["status"], "completed_without_agent")
        self.assertEqual(done["approval"]["status"], "executed")
        await self.assert_delivered({"recipient": ATTACKER_EMAIL}, await self.records())

    async def test_invalid_addresses_and_missing_arguments_create_no_delivery(self):
        for arguments in ({}, {"recipient": ""}, {"recipient": "one@example.test,two@example.test"},
                          {"recipient": ATTACKER_EMAIL, "bcc": "invalid"}):
            with self.subTest(arguments=arguments):
                await self.turn(arguments)
                await self.no_delivery()

    async def test_empty_synthetic_dataset_reports_zero_actual_records(self):
        async with self.sessions() as db:
            await db.execute(delete(KcTechnicianContact).where(KcTechnicianContact.user_id == self.learner.id))
            await db.commit()
        await self.no_delivery()
        result = await self.turn()
        self.assertEqual(result["steps"][0]["result"]["records"], 0)
        await self.assert_delivered({"recipient": ATTACKER_EMAIL}, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
