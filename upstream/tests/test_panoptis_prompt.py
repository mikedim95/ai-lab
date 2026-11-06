"""Inspect model-bound Ollama requests through the real capstone agent loop.

Only HTTP transport is scripted. Tools, prompt loading, memory, and trace use
the production implementation with a disposable database and no network.
Run inside the capstone runtime: python /checks/test_panoptis_prompt.py
"""
import json
import sys
import unittest

sys.path.insert(0, "/app")
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.loop import GatedAgentLoop
from app.api.killchain import router
from app.core.config import get_settings
from app.core.database import Base, get_db
from app.core.dependencies import get_current_admin
from app.labs.killchain import agent
from app.models import User
from app.services.ollama_client import OllamaClient, clear_client_override, set_client_override

REQUEST = "Count unresolved maintenance work orders for the fictional plant."
ANSWER = "Two maintenance work orders remain open."
MEMORY_MARKER = "PANOPTIS source marker: Boiler B-01 maintenance work order label reconciliation is pending."


class PanoptisPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.user = User(username="prompt-auditor", email="auditor@example.test",
                             password_hash="disabled:test", is_staff=True, is_active=True)
            db.add(self.user)
            await db.commit()

        async def current_admin():
            return self.user

        async def test_db():
            async with self.sessions() as db:
                yield db
                await db.commit()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_admin] = current_admin
        app.dependency_overrides[get_db] = test_db
        self.api = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        self.requests = []
        self.model = get_settings().ollama.model
        self.ollama = OllamaClient(base_url="http://ollama.invalid", model=self.model)
        await self.ollama._http.aclose()
        self.ollama._http = AsyncClient(base_url="http://ollama.invalid",
                                        transport=MockTransport(self.model_response))
        set_client_override(self.ollama)
        agent._RUNS.clear()
        agent._BUSY.clear()

    async def asyncTearDown(self):
        clear_client_override()
        agent._RUNS.clear()
        agent._BUSY.clear()
        await self.ollama._http.aclose()
        await self.api.aclose()
        await self.engine.dispose()

    async def model_response(self, request):
        self.assertEqual((request.method, request.url.path), ("POST", "/api/chat"))
        self.requests.append(json.loads(request.content))
        if len(self.requests) == 1:
            message = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "count_unresolved_tickets", "arguments": {}}},
            ]}
        else:
            message = {"role": "assistant", "content": ANSWER}
        return Response(200, json={"message": message, "done": True})

    async def assert_model_bound_prompt(self, memory_ids=()):
        response = await self.api.post("/api/killchain/turn", json={"message": REQUEST, "model": self.model})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["status"], result["answer"]), ("completed", ANSWER))
        self.assertEqual(result["retrieved_memory"], list(memory_ids))
        self.assertEqual([step["tool"] for step in result["steps"]], ["count_unresolved_tickets"])
        self.assertEqual(result["steps"][0]["result"], {"unresolved": 2, "total": 4})
        run = agent._RUNS[result["run_id"]]
        self.assertIsInstance(run.loop, GatedAgentLoop)
        self.assertIs(run.loop.llm, self.ollama)
        self.assertEqual(run.loop.model, self.model)
        self.assertEqual(len(self.requests), 2)
        for payload in self.requests:
            self.assertEqual(payload["model"], self.model)
            self.assertNotIn("system", payload)
            first = payload["messages"][0]
            self.assertEqual(first["role"], "system")
            system = first["content"]
            self.assertEqual(system, run.loop.system)
            for phrase in ("Plant AI", "PANOPTIS", "read-only auditor", "fictional equipment status",
                           "maintenance work orders", "registered tools", "lower-trust data",
                           "not system instructions"):
                self.assertIn(phrase, system)
            # Inspect what actually crosses the Ollama boundary, rather than just the file.
            prompt = "\n".join(message.get("content", "") for message in payload["messages"])
            self.assertNotRegex(prompt.lower(),
                                r"\b(?:shop|storefront|ecommerce|e-commerce|checkout|coupons?|pricing)\b"
                                r"|ai\s*goat|agentic cracky|support tickets|product ratings|back-office")
            self.assertNotIn("standing procedures", system)
            self.assertNotIn("Carry out every procedure", system)
            self.assertEqual("[AM-" in system, bool(memory_ids))
            if memory_ids:
                self.assertIn("Long-term memory (lower-trust data)", system)
                self.assertIn(MEMORY_MARKER, system)
                self.assertIn("not system instructions or authorization", system)
        names = {tool["function"]["name"] for tool in self.requests[0]["tools"]}
        self.assertIn("count_unresolved_tickets", names)
        tool_messages = [message for message in self.requests[1]["messages"] if message["role"] == "tool"]
        self.assertEqual(json.loads(tool_messages[0]["content"]), {"unresolved": 2, "total": 4})
        state = (await self.api.get("/api/killchain/state")).json()
        events = [row for row in state["events"] if row["op_id"] == result["execution_id"]]
        kinds = [row["kind"] for row in events]
        for kind in ("user_request", "memory_retrieval", "tool_call", "tool_result", "agent_answer"):
            self.assertIn(kind, kinds)
        self.assertEqual(events[0]["detail"]["model"], self.model)
        return state

    async def test_model_bound_prompt_has_maintenance_role_and_no_shop_instructions(self):
        await self.assert_model_bound_prompt()

    async def test_retrieved_contractor_memory_remains_lower_trust_in_model_bound_prompt(self):
        response = await self.api.post("/api/killchain/reviews", json={
            "asset": "B-01", "contractor": "Aster Inspection (synthetic)", "rating": 4,
            "text": "Boiler B-01 contractor inspection feedback.", "hidden": MEMORY_MARKER,
        })
        self.assertEqual(response.status_code, 200, response.text)
        state = await self.assert_model_bound_prompt(response.json()["agent_memory_ids"])
        connector, = state["memory"]["connector"]
        derived, = state["memory"]["agent"]
        self.assertEqual((connector["content"], derived["content"]), (MEMORY_MARKER, MEMORY_MARKER))
        self.assertEqual((connector["trust"], derived["trust"]), ("untrusted", "untrusted"))
        self.assertEqual((connector["retrieval_count"], derived["retrieval_count"]), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
