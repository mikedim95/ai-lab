"""PANOPTIS baseline/storage checks using disposable SQLite files, never learner data.

Run inside the capstone container: python /checks/test_panoptis_seed.py
"""
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, "/app")
from sqlalchemy import event, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from app.labs.killchain import seed, service
from app.models import User
from app.models.killchain import (
    KcAgentMemory, KcApproval, KcConnectorMemory, KcCoupon, KcCustomer, KcProduct,
    KcReview, KcSeedMarker, KcState, KcTechnicianContact, KcTicket,
)

NEW_TABLES = {"kc_seed_markers", "kc_technician_contacts"}


class PanoptisSeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="panoptis-seed-test-")
        self.url = "sqlite+aiosqlite:///" + str(Path(self.directory.name) / "learner.sqlite3")
        self.open_engine()
        # Start with the schema that existing learners already have.
        legacy_tables = [table for table in database.Base.metadata.sorted_tables
                         if table.name not in NEW_TABLES]
        async with self.engine.begin() as connection:
            await connection.run_sync(lambda conn: database.Base.metadata.create_all(conn, tables=legacy_tables))

    def open_engine(self):
        self.engine = create_async_engine(self.url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.directory.cleanup()

    async def bootstrap(self):
        # Exercise the real additive bootstrap against the disposable database.
        with patch.object(database, "engine", self.engine):
            await database.init_db()

    async def learner(self, name, *, existing=False):
        async with self.sessions() as db:
            row = User(username=name, email=name + "@example.test", password_hash="disabled:test",
                       is_staff=True, is_active=True)
            db.add(row)
            await db.flush()
            if existing:
                db.add(KcState(user_id=row.id, mode="guardrailed", epoch=7,
                               conversation=[{"role": "user", "content": "Keep my learner history."}]))
            await db.commit()
            return row.id

    async def rows(self, model, user_id):
        async with self.sessions() as db:
            result = await db.execute(select(model).where(model.user_id == user_id).order_by(model.id))
            return list(result.scalars())

    async def records(self, model, user_id):
        async with self.sessions() as db:
            result = await db.execute(select(model.__table__).where(model.user_id == user_id).order_by(model.id))
            return [dict(row) for row in result.mappings()]

    async def start(self, user_id):
        async with self.sessions() as db:
            return await seed.ensure_baseline(db, user_id)

    async def test_fresh_baseline_has_assets_work_orders_and_clean_contractor_feedback(self):
        await self.bootstrap()
        user_id = await self.learner("fresh")
        with patch.object(seed, "load_shop", side_effect=AssertionError("Do not mirror the shop by default")):
            state = await self.start(user_id)
        self.assertEqual((state.mode, state.epoch, state.conversation), ("vulnerable", 1, []))
        assets = await self.rows(KcProduct, user_id)
        self.assertEqual({row.sku: row.name for row in assets},
                         {"B-01": "Boiler B-01", "G-01": "Generator G-01"})
        self.assertTrue(all(row.price == Decimal("0.00") and not row.coupon_eligible for row in assets))
        feedback = await self.rows(KcReview, user_id)
        self.assertEqual(len(feedback), 4)
        self.assertEqual({row.product_id for row in feedback}, {row.id for row in assets})
        self.assertTrue(all(row.seeded and "synthetic" in row.author and "<!--" not in row.body
                            for row in feedback))
        orders = await self.rows(KcTicket, user_id)
        self.assertEqual({row.subject.split(":", 1)[0] for row in orders},
                         {"WO-B01-001", "WO-B01-002", "WO-G01-001", "WO-G01-002"})
        self.assertEqual(sorted(row.status for row in orders), ["open", "open", "resolved", "resolved"])
        self.assertTrue(all(row.seeded and "Fictional PANOPTIS" in row.body for row in orders))
        self.assertEqual(await self.rows(KcCustomer, user_id), [])
        self.assertEqual(await self.rows(KcConnectorMemory, user_id), [])
        self.assertEqual(await self.rows(KcAgentMemory, user_id), [])
        self.assertEqual(len(await self.rows(KcSeedMarker, user_id)), 1)

    async def test_restricted_synthetic_contacts_are_private_and_isolated_by_learner(self):
        await self.bootstrap()
        first = await self.learner("first")
        second = await self.learner("second")
        await self.start(first)
        await self.start(second)
        contacts = await self.rows(KcTechnicianContact, first)
        others = await self.rows(KcTechnicianContact, second)
        self.assertEqual(len(contacts), 4)
        self.assertEqual(len(others), 4)
        self.assertTrue({row.id for row in contacts}.isdisjoint({row.id for row in others}))
        self.assertTrue(all(row.user_id == first and row.classification == "restricted" and row.synthetic
                            and row.email.endswith("@panoptis.example.test") and row.extension.startswith("SIM-")
                            and set(row.asset_scope) <= {"B-01", "G-01"} for row in contacts))
        async with self.sessions() as db:
            public = json.dumps(await service.snapshot(db, first))
        for contact in contacts:
            self.assertNotIn(contact.email, public)
            self.assertNotIn(contact.extension, public)
        first_assets = {row.id for row in await self.rows(KcProduct, first)}
        second_assets = {row.id for row in await self.rows(KcProduct, second)}
        self.assertTrue(first_assets.isdisjoint(second_assets))
        self.assertTrue(all(row.product_id in first_assets for row in await self.rows(KcReview, first)))
        self.assertTrue(all(row.product_id in second_assets for row in await self.rows(KcReview, second)))

    async def test_additive_bootstrap_and_upgrade_preserve_existing_learner_data(self):
        user_id = await self.learner("legacy", existing=True)
        async with self.sessions() as db:
            asset = KcProduct(user_id=user_id, sku="B-01", name="My existing B-01 record",
                              price=Decimal("12.00"), coupon_eligible=True)
            db.add(asset)
            await db.flush()
            db.add_all([
                KcReview(user_id=user_id, product_id=asset.id, author="Learner", rating=4,
                         body="My original inspection note", seeded=False),
                KcTicket(user_id=user_id, subject=seed.TICKETS[1][0], body="My existing work order",
                         status="resolved", seeded=False),
                KcCustomer(user_id=user_id, name="Synthetic legacy customer", email="legacy@example.test",
                           order_ref="OLD-1", card_brand="test", card_number="4242424242424242",
                           billing_city="Test city", billing_postcode="TEST"),
                KcConnectorMemory(user_id=user_id, source_type="review", source_id=1,
                                  content="Existing learner memory", provenance={"learner": True}),
                KcAgentMemory(user_id=user_id, content="Existing agent notes", topics=["history"]),
                KcApproval(user_id=user_id, tool="historical_action", arguments={"keep": True},
                           args_hash="stored-hash", action_type="historical", status="pending"),
            ])
            await db.commit()
        preserved = (KcState, KcProduct, KcReview, KcTicket, KcCustomer,
                     KcConnectorMemory, KcAgentMemory, KcApproval)
        before = {model: await self.records(model, user_id) for model in preserved}
        async with self.engine.connect() as connection:
            old_tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        self.assertTrue(NEW_TABLES.isdisjoint(old_tables))
        await self.bootstrap()
        async with self.engine.connect() as connection:
            new_tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names()))
        self.assertEqual(new_tables - old_tables, NEW_TABLES)
        with patch.object(seed, "wipe", side_effect=AssertionError("An upgrade must not wipe data")):
            state = await self.start(user_id)
        self.assertEqual((state.mode, state.epoch), ("guardrailed", 7))
        for model, original in before.items():
            after = {row["id"]: row for row in await self.records(model, user_id)}
            for row in original:
                self.assertEqual(after[row["id"]], row, model.__name__)
        self.assertEqual({row.sku for row in await self.rows(KcProduct, user_id)}, {"B-01", "G-01"})
        self.assertEqual(len(await self.rows(KcTechnicianContact, user_id)), 4)
        self.assertEqual(await self.rows(KcCoupon, user_id), [], "Upgrade must not restore legacy coupons")

    async def test_repeat_visits_preserve_edits_and_do_not_restore_removed_fixture_rows(self):
        await self.bootstrap()
        user_id = await self.learner("edited")
        await self.start(user_id)
        async with self.sessions() as db:
            asset = (await db.execute(select(KcProduct).where(KcProduct.user_id == user_id))).scalars().first()
            asset.name = "My edited asset"
            contact = (await db.execute(select(KcTechnicianContact).where(
                KcTechnicianContact.user_id == user_id))).scalars().first()
            contact.extension = "SIM-EDITED"
            feedback = (await db.execute(select(KcReview).where(KcReview.user_id == user_id))).scalars().first()
            await db.delete(feedback)
            await db.commit()
        models = (KcProduct, KcReview, KcTicket, KcTechnicianContact, KcSeedMarker)
        before = {model: await self.records(model, user_id) for model in models}
        await self.start(user_id)
        await self.start(user_id)
        for model, rows in before.items():
            self.assertEqual(await self.records(model, user_id), rows)

    async def test_file_database_survives_reopening_without_duplicate_fixtures(self):
        await self.bootstrap()
        user_id = await self.learner("persistent", existing=True)
        await self.start(user_id)
        models = (KcState, KcProduct, KcReview, KcTicket, KcTechnicianContact, KcSeedMarker)
        before = {model: await self.records(model, user_id) for model in models}
        await self.engine.dispose()
        self.open_engine()
        await self.bootstrap()
        await self.start(user_id)
        for model, rows in before.items():
            self.assertEqual(await self.records(model, user_id), rows)

    async def test_concurrent_upgrade_claims_one_baseline(self):
        user_id = await self.learner("concurrent", existing=True)
        await self.bootstrap()
        states = await asyncio.gather(self.start(user_id), self.start(user_id))
        self.assertTrue(all(state.epoch == 7 and state.mode == "guardrailed" for state in states))
        for model, count in ((KcProduct, 2), (KcReview, 4), (KcTicket, 4),
                             (KcTechnicianContact, 4), (KcSeedMarker, 1)):
            self.assertEqual(len(await self.rows(model, user_id)), count)

    async def test_failed_upgrade_rolls_back_partial_records_and_seed_claim(self):
        user_id = await self.learner("retry", existing=True)
        await self.bootstrap()
        original = seed._seed_rows

        async def fail_after_inserting(db, owner, **options):
            await original(db, owner, **options)
            raise RuntimeError("Simulated storage failure")

        async with self.sessions() as db:
            with patch.object(seed, "_seed_rows", side_effect=fail_after_inserting):
                with self.assertRaisesRegex(RuntimeError, "Simulated storage failure"):
                    await seed.ensure_baseline(db, user_id)
            await db.rollback()
        self.assertEqual(await self.rows(KcSeedMarker, user_id), [])
        self.assertEqual(await self.rows(KcProduct, user_id), [])
        self.assertEqual(await self.rows(KcTechnicianContact, user_id), [])
        state = await self.start(user_id)
        self.assertEqual((state.mode, state.epoch), ("guardrailed", 7))
        self.assertEqual(len(await self.rows(KcTechnicianContact, user_id)), 4)

    async def test_explicit_hard_reset_is_learner_scoped_and_can_reseed(self):
        await self.bootstrap()
        first = await self.learner("reset_first")
        second = await self.learner("reset_second", existing=True)
        await self.start(first)
        await self.start(second)
        models = (KcState, KcProduct, KcReview, KcTicket, KcTechnicianContact, KcSeedMarker)
        other_before = {model: await self.records(model, second) for model in models}
        async with self.sessions() as db:
            await service.hard_reset(db, first)
            await service.hard_reset(db, first)
        self.assertEqual(len(await self.rows(KcProduct, first)), 2)
        self.assertEqual(len(await self.rows(KcReview, first)), 4)
        self.assertEqual(len(await self.rows(KcTicket, first)), 4)
        self.assertEqual(len(await self.rows(KcTechnicianContact, first)), 4)
        self.assertEqual(len(await self.rows(KcSeedMarker, first)), 1)
        self.assertEqual((await self.rows(KcState, first))[0].epoch, 3)
        for model, rows in other_before.items():
            self.assertEqual(await self.records(model, second), rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
