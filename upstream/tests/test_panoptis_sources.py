"""PANOPTIS source regressions with real extraction and disposable SQLite.

Run inside the capstone runtime: python /checks/test_panoptis_sources.py
Hidden test content is inert marker text. No model or agent tools are invoked.
"""
import hashlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "/app")
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.killchain import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_admin
from app.labs.killchain import invoice, memory, seed
from app.labs.killchain.constants import (
    INSPECTION_FIXTURE, INSPECTION_FIXTURE_NAME, INVOICE_FIXTURE,
)
from app.labs.killchain.pdf import analyze_pdf
from app.models import User
from app.models.killchain import KcReview

FEEDBACK = "Boiler B-01 inspection records reconciled by the contractor."
CONTRACTOR = "Aster Inspection (synthetic)"
SUBJECT = "WO-B01-002: Boiler B-01 inspection record follow-up"
DESCRIPTION = "Reconcile the contractor's inspection notes with the maintenance record."
MARKERS = ("PANOPTIS inspection marker alpha.", "PANOPTIS inspection marker beta.")


def inspection_pdf(markers=MARKERS):
    # Only the fixture's input text changes; the real PDF builder and extractor run.
    with patch.object(invoice, "PROCEDURES", dict(zip(invoice.HIDDEN_PROCEDURES, markers))):
        return invoice.build_inspection_pdf()


class PanoptisSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            self.learner = User(username="sources", email="sources@example.test",
                                password_hash="disabled:test", is_staff=True, is_active=True)
            self.other = User(username="other", email="other@example.test",
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

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()

    async def state(self):
        response = await self.client.get("/api/killchain/state")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def feedback(self, **overrides):
        body = {"asset": "B-01", "contractor": CONTRACTOR, "rating": 4, "text": FEEDBACK}
        body.update(overrides)
        response = await self.client.post("/api/killchain/reviews", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def work_order(self, data=None):
        files = {} if data is None else {"file": (INSPECTION_FIXTURE_NAME, data, "application/pdf")}
        response = await self.client.post("/api/killchain/tickets",
                                          data={"subject": SUBJECT, "body": DESCRIPTION}, files=files)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_contractor_feedback_stores_hidden_html_and_uses_original_memory(self):
        marker = "PANOPTIS contractor service feedback marker."
        result = await self.feedback(hidden=marker)
        state = await self.state()
        feedback = next(row for row in state["reviews"] if row["id"] == result["review_id"])
        self.assertEqual((feedback["sku"], feedback["author"], feedback["text"]),
                         ("B-01", CONTRACTOR, FEEDBACK))
        self.assertEqual(feedback["raw"], FEEDBACK + "\n<!-- " + marker + " -->")
        self.assertTrue(feedback["has_hidden_markup"])
        self.assertNotIn(marker, feedback["text"])
        async with self.sessions() as db:
            stored = await db.get(KcReview, result["review_id"])
            self.assertEqual(stored.body, feedback["raw"])
        connector, = state["memory"]["connector"]
        self.assertEqual((connector["source_type"], connector["source_id"], connector["content"]),
                         ("review", result["review_id"], marker))
        self.assertEqual(connector["provenance"]["source_kind"], "contractor_service_feedback")
        self.assertEqual(connector["provenance"]["asset"], "B-01")
        self.assertEqual(connector["provenance"]["contractor"], CONTRACTOR)
        self.assertEqual(connector["provenance"]["extractor"], "html-comment reader")
        self.assertTrue(connector["provenance"]["hidden"])
        agent, = state["memory"]["agent"]
        self.assertEqual((agent["content"], agent["connector_memory_id"]), (marker, connector["id"]))
        self.assertEqual([row["kind"] for row in state["events"]],
                         ["review_submitted", "content_extracted", "connector_memory_write", "agent_memory_write"])

    async def test_inline_comments_in_generator_feedback_are_also_extracted(self):
        result = await self.feedback(asset="G-01", contractor="Helios Field Services (synthetic)",
                                     text="Generator G-01 service notes.\n<!-- marker one -->\n<!-- marker two -->")
        state = await self.state()
        feedback = next(row for row in state["reviews"] if row["id"] == result["review_id"])
        self.assertEqual(feedback["text"], "Generator G-01 service notes.")
        self.assertEqual(state["memory"]["connector"][0]["content"], "marker one\n\nmarker two")
        self.assertEqual({row["content"] for row in state["memory"]["agent"]}, {"marker one", "marker two"})

    async def test_feedback_cache_is_reused_without_losing_source_provenance(self):
        first = await self.feedback(hidden="Contractor cache marker.")
        second = await self.feedback(hidden="Contractor cache marker.")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        state = await self.state()
        self.assertEqual(len(state["memory"]["cache"]), 1)
        self.assertEqual(state["memory"]["cache"][0]["hits"], 1)
        self.assertEqual({row["source_id"] for row in state["memory"]["connector"]},
                         {first["review_id"], second["review_id"]})

    async def test_work_order_inspection_pdf_is_extracted_from_uploaded_bytes(self):
        data = inspection_pdf()
        result = await self.work_order(data)
        attachment_id = result["attachment"]["attachment_id"]
        state = await self.state()
        work_order = next(row for row in state["tickets"] if row["id"] == result["ticket_id"])
        self.assertEqual((work_order["subject"], work_order["body"], work_order["status"]),
                         (SUBJECT, DESCRIPTION, "open"))
        self.assertEqual(work_order["attachment"]["filename"], INSPECTION_FIXTURE_NAME)
        self.assertEqual(work_order["attachment"]["hidden_runs"], 2)
        evidence = (await self.client.get(f"/api/killchain/attachments/{attachment_id}")).json()
        self.assertIn("MAINTENANCE INSPECTION", evidence["visible_text"])
        self.assertIn("WO-B01-002", evidence["visible_text"])
        self.assertEqual(evidence["hidden_text"], "\n".join(MARKERS))
        for marker in MARKERS:
            self.assertNotIn(marker, evidence["visible_text"])
            self.assertIn(marker, evidence["extracted_text"])
        hidden = [span for span in evidence["spans"] if span["hidden"]]
        self.assertTrue(all(span["color"] == "#ffffff" for span in hidden))
        connector, = state["memory"]["connector"]
        self.assertEqual((connector["source_type"], connector["source_id"], connector["content"]),
                         ("ticket_attachment", attachment_id, "\n".join(MARKERS)))
        self.assertEqual(connector["provenance"]["source_kind"], "maintenance_inspection")
        self.assertEqual(connector["provenance"]["work_order_id"], result["ticket_id"])
        self.assertEqual(connector["provenance"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(connector["provenance"]["extractor"], "pdf text-layer reader")
        agent, = state["memory"]["agent"]
        self.assertEqual((agent["content"], agent["connector_memory_id"]),
                         (" ".join(MARKERS), connector["id"]))
        self.assertEqual([row["kind"] for row in state["events"]][:3],
                         ["ticket_created", "attachment_uploaded", "content_extracted"])
        download = await self.client.get(f"/api/killchain/attachments/{attachment_id}/file")
        self.assertEqual(download.content, data)

    async def test_inspection_cache_follows_actual_pdf_content(self):
        first = await self.work_order(inspection_pdf())
        second = await self.work_order(inspection_pdf())
        changed = ("Changed inspection marker alpha.", "Changed inspection marker beta.")
        third = await self.work_order(inspection_pdf(changed))
        self.assertFalse(first["attachment"]["cache_hit"])
        self.assertTrue(second["attachment"]["cache_hit"])
        self.assertFalse(third["attachment"]["cache_hit"])
        state = await self.state()
        self.assertEqual(len(state["memory"]["cache"]), 2)
        self.assertEqual(state["memory"]["connector"][-1]["content"], "\n".join(changed))
        self.assertEqual(state["memory"]["connector"][-1]["source_id"],
                         third["attachment"]["attachment_id"])

    async def test_visible_feedback_and_clean_work_orders_do_not_write_memory(self):
        await self.feedback()
        no_file = await self.work_order()
        self.assertIsNone(no_file["attachment"])
        with patch.object(invoice, "HIDDEN_PROCEDURES", ()):
            clean_pdf = invoice.build_inspection_pdf()
        self.assertEqual(analyze_pdf(clean_pdf).hidden_text, "")
        await self.work_order(clean_pdf)
        state = await self.state()
        self.assertEqual(state["memory"], {"connector": [], "cache": [], "agent": []})

    async def test_both_sources_survive_agent_memory_cleanup_and_rederive(self):
        await self.feedback(hidden="Contractor persistence marker.")
        await self.work_order(inspection_pdf())
        before = await self.state()
        response = await self.client.post("/api/killchain/cleanup/agent_memory")
        self.assertEqual(response.status_code, 200, response.text)
        cleared = await self.state()
        self.assertEqual(cleared["memory"]["connector"], before["memory"]["connector"])
        self.assertEqual(cleared["memory"]["agent"], [])
        async with self.sessions() as db:
            await memory.sync_agent_memory(db, self.learner.id, "source-regression", reason="resync")
            await db.commit()
        restored = await self.state()
        self.assertEqual({row["content"] for row in restored["memory"]["agent"]},
                         {"Contractor persistence marker.", " ".join(MARKERS)})

    async def test_sources_and_attachments_are_isolated_by_learner(self):
        feedback = await self.feedback(hidden="Private source marker.")
        work_order = await self.work_order(inspection_pdf())
        own = await self.state()
        asset_id = next(row["product_id"] for row in own["reviews"] if row["id"] == feedback["review_id"])
        self.identity = self.other
        other = await self.state()
        self.assertEqual(other["memory"], {"connector": [], "cache": [], "agent": []})
        attachment_id = work_order["attachment"]["attachment_id"]
        for suffix in ("", "/file"):
            response = await self.client.get(f"/api/killchain/attachments/{attachment_id}" + suffix)
            self.assertEqual(response.status_code, 404)
        response = await self.client.post("/api/killchain/reviews",
                                          json={"asset": str(asset_id), "rating": 4, "text": FEEDBACK})
        self.assertEqual(response.status_code, 404)
        response = await self.client.post(f"/api/killchain/tickets/{work_order['ticket_id']}/attachment",
                                          files={"file": (INSPECTION_FIXTURE_NAME, inspection_pdf(), "application/pdf")})
        self.assertEqual(response.status_code, 404)

    async def test_legacy_product_author_fields_and_existing_work_order_attachment(self):
        response = await self.client.post("/api/killchain/reviews", json={
            "product": "B-01", "author": CONTRACTOR, "rating": 4, "text": FEEDBACK, "hidden": "Legacy marker.",
        })
        self.assertEqual(response.status_code, 200, response.text)
        order = await self.work_order()
        response = await self.client.post(f"/api/killchain/tickets/{order['ticket_id']}/attachment",
                                          files={"file": (INSPECTION_FIXTURE_NAME, inspection_pdf(), "application/pdf")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len((await self.state())["memory"]["connector"]), 2)

    async def test_fixture_is_reproducible_and_uses_the_existing_download_url(self):
        data = INSPECTION_FIXTURE.read_bytes()
        self.assertEqual(data, invoice.build_inspection_pdf())
        self.assertEqual(data, invoice.build_invoice_pdf())
        self.assertEqual(INVOICE_FIXTURE, INSPECTION_FIXTURE)
        analysis = analyze_pdf(data)
        self.assertIn("MAINTENANCE INSPECTION", analysis.visible_text)
        self.assertIn("Boiler B-01", analysis.visible_text)
        self.assertNotIn("INVOICE", analysis.visible_text)
        self.assertNotIn("Total due", analysis.visible_text)
        self.assertEqual(len([span for span in analysis.spans if span.hidden]), 2)
        examples = (await self.client.get("/api/killchain/examples")).json()
        self.assertEqual(examples["fixture"], {"filename": INSPECTION_FIXTURE_NAME,
                                              "url": "/api/killchain/fixtures/invoice.pdf"})
        response = await self.client.get(examples["fixture"]["url"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, data)
        self.assertIn(INSPECTION_FIXTURE_NAME, response.headers["content-disposition"])
        self.assertEqual(response.headers["content-type"], "application/pdf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
