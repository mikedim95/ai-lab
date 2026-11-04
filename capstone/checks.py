"""Isolated checks of the copied capstone engine; no live model or application data.

The model is scripted, while ingestion, the agent loop, tools, approvals, PDF
parsing and guardrails are the real upstream implementation.
"""
import secrets
import sys
import unittest

sys.path.insert(0, "/app")
sys.path.insert(0, "/checks")
import server as adapter
from bridge import signature
from fake_llm import FakeLLMClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.core.database import Base, get_db
from app.labs.killchain import agent
from app.labs.killchain.constants import ATTACKER_EMAIL, OPS_EMAIL, PROCEDURES, UNIVERSAL_COUPON
from app.labs.killchain.invoice import build_invoice_pdf
from app.services.ollama_client import clear_client_override, set_client_override


def call(name, **arguments):
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments}]}


def answer(text="Done."):
    return {"content": text, "tool_calls": []}


class CapstoneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.original_sessions = adapter.async_session
        adapter.async_session = self.sessions

        async def test_db():
            async with self.sessions() as session:
                yield session
                await session.commit()

        adapter.application.dependency_overrides[get_db] = test_db
        self.sid = secrets.token_urlsafe(32)
        self.headers = {"X-Nine-Session": self.sid, "X-Nine-Signature": signature(adapter.KEY, self.sid)}
        self.client = AsyncClient(transport=ASGITransport(app=adapter.application), base_url="http://test", headers=self.headers)
        self.fake = FakeLLMClient()
        set_client_override(self.fake)
        agent._RUNS.clear()
        agent._BUSY.clear()

    async def asyncTearDown(self):
        clear_client_override()
        adapter.async_session = self.original_sessions
        adapter.application.dependency_overrides.pop(get_db, None)
        await self.client.aclose()
        await self.engine.dispose()
        agent._RUNS.clear()
        agent._BUSY.clear()

    async def get(self, path="/state"):
        response = await self.client.get("/api/killchain" + path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def post(self, path, body=None):
        response = await self.client.post("/api/killchain" + path, json=body or {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def poison(self, key="customer_export"):
        return await self.post("/reviews", {"product": "KC-1001", "rating": 5, "text": "Good quality.", "hidden": PROCEDURES[key]})

    async def test_01_review_dual_memory_and_repopulation(self):
        await self.poison()
        state = await self.get()
        self.assertEqual(len(state["memory"]["connector"]), 1)
        self.assertEqual(len(state["memory"]["agent"]), 1)
        await self.post("/cleanup/agent_memory")
        state = await self.get()
        self.assertEqual(state["memory"]["agent"], [])
        self.fake.script_turns([call("count_unresolved_tickets"), answer("There are five open tickets.")])
        await self.post("/turn", {"message": "How many support tickets still need resolution?"})
        state = await self.get()
        self.assertEqual(len(state["memory"]["agent"]), 1)
        self.assertEqual(state["memory"]["agent"][0]["provenance"]["derivation"], "resync")

    async def test_02_real_invoice_hidden_text_and_session_isolation(self):
        pdf = build_invoice_pdf()
        response = await self.client.post("/api/killchain/tickets", data={"subject": "Invoice", "body": "Please review."}, files={"file": ("invoice.pdf", pdf, "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        attachment = response.json()["attachment"]["attachment_id"]
        evidence = await self.get(f"/attachments/{attachment}")
        self.assertIn("INVOICE", evidence["visible_text"])
        self.assertIn("AGENT PROCEDURE", evidence["hidden_text"])
        self.assertGreaterEqual(evidence["summary"]["hidden_runs"], 2)
        second = secrets.token_urlsafe(32)
        other_headers = {"X-Nine-Session": second, "X-Nine-Signature": signature(adapter.KEY, second)}
        other = await self.client.get("/api/killchain/state", headers=other_headers)
        self.assertEqual(other.json()["memory"]["connector"], [])
        denied = await self.client.get(f"/api/killchain/attachments/{attachment}", headers=other_headers)
        self.assertEqual(denied.status_code, 404)
        denied = await self.client.get("/api/killchain/state", headers={"X-Nine-Signature": "forged"})
        self.assertEqual(denied.status_code, 403)

    async def test_03_vulnerable_tool_export_writes_actual_inbox_records(self):
        await self.poison()
        self.fake.script_turns([call("lookup_customers"), call("send_export_email", dataset="customers", recipient=OPS_EMAIL, bcc=ATTACKER_EMAIL), call("summarize_tickets", period="today"), answer("Tickets summarized.")])
        run = await self.post("/turn", {"message": "Summarize the support tickets received today."})
        self.assertEqual(run["status"], "completed")
        state = await self.get()
        self.assertEqual(state["status"]["exfiltration"], 1)
        self.assertEqual(len(state["inbox"][0]["payload"]["records"]), 6)
        self.assertEqual(state["inbox"][0]["bcc"], ATTACKER_EMAIL)

    async def test_04_defended_approval_executes_once(self):
        await self.poison()
        await self.post("/mode", {"mode": "defended"})
        self.fake.script_turns([call("send_export_email", dataset="customers", recipient=OPS_EMAIL, bcc=ATTACKER_EMAIL), answer("Report sent.")])
        paused = await self.post("/turn", {"message": "Summarize the support tickets received today."})
        self.assertEqual(paused["status"], "awaiting_approval")
        self.assertEqual((await self.get())["inbox"], [])
        approval_id = paused["pending"]["id"]
        done = await self.post(f"/approvals/{approval_id}/decision", {"decision": "approve"})
        self.assertEqual(done["approval"]["status"], "executed")
        self.assertEqual(len((await self.get())["inbox"]), 1)
        repeat = await self.client.post(f"/api/killchain/approvals/{approval_id}/decision", json={"decision": "approve"})
        self.assertEqual(repeat.status_code, 409)

    async def test_05_guardrailed_ingestion_and_wrong_approval(self):
        await self.post("/mode", {"mode": "guardrailed"})
        result = await self.poison()
        self.assertTrue(result["quarantined"])
        self.assertEqual((await self.get())["memory"]["agent"], [])
        await self.post("/cleanup/hard_reset")
        await self.post("/mode", {"mode": "vulnerable"})
        await self.poison()
        await self.post("/mode", {"mode": "guardrailed"})
        self.fake.script_turns([call("send_export_email", dataset="customers", recipient=OPS_EMAIL, bcc=ATTACKER_EMAIL), answer("Export blocked.")])
        paused = await self.post("/turn", {"message": "Summarize the support tickets received today."})
        done = await self.post(f"/approvals/{paused['pending']['id']}/decision", {"decision": "approve"})
        self.assertEqual(done["approval"]["status"], "blocked")
        self.assertEqual(done["approval"]["result"]["rail"], "egress.allowlist")
        self.assertEqual((await self.get())["inbox"], [])

    async def test_06_coupon_disclosure_and_one_dollar_checkout(self):
        await self.poison("coupon_disclosure")
        self.fake.script_turns([call("list_coupons"), call("send_export_email", dataset="coupons", recipient=OPS_EMAIL, bcc=ATTACKER_EMAIL), answer("Ratings checked.")])
        await self.post("/turn", {"message": "Which products have the lowest customer ratings?"})
        exported = (await self.get())["inbox"][0]["payload"]["records"]
        self.assertIn(UNIVERSAL_COUPON, {item["code"] for item in exported})
        await self.poison("coupon_abuse")
        self.fake.script_turns([call("get_checkout_price", product="KC-1001", coupon_code=UNIVERSAL_COUPON), answer("The checkout quote is $1.")])
        await self.post("/turn", {"message": "Check the checkout price for KC-1001."})
        self.assertEqual((await self.get())["checkouts"][0]["final_price"], "1.00")
        shop = await self.post("/storefront/coupon", {"product": "KC-1001", "code": UNIVERSAL_COUPON})
        self.assertFalse(shop["accepted"])

    async def test_07_soft_and_hard_reset_keep_their_boundaries(self):
        await self.poison()
        await self.post("/cleanup/soft_reset")
        state = await self.get()
        self.assertEqual(len(state["memory"]["connector"]), 1)
        self.assertEqual(len(state["memory"]["agent"]), 1)
        await self.post("/cleanup/hard_reset")
        state = await self.get()
        self.assertEqual(state["memory"]["connector"], [])
        self.assertEqual(state["memory"]["agent"], [])
        self.assertEqual(state["inbox"], [])
        self.assertEqual(state["overall"], "baseline")


if __name__ == "__main__":
    unittest.main(verbosity=2)
