"""The seeded baseline. One function builds it, and both first use and hard reset call that function."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.labs.killchain.constants import (
    INTERNAL_DOMAIN,
    MODE_VULNERABLE,
    UNIVERSAL_COUPON,
    UNIVERSAL_PRICE,
)
from app.labs.killchain.shop_data import ShopSnapshot, load_shop
from app.models.killchain import (
    KcAgentMemory,
    KcApproval,
    KcAttachment,
    KcBoilerState,
    KcCheckout,
    KcConnectorCache,
    KcConnectorMemory,
    KcCoupon,
    KcCustomer,
    KcEvent,
    KcMail,
    KcProduct,
    KcReview,
    KcSeedMarker,
    KcState,
    KcTechnicianContact,
    KcTicket,
)

# PANOPTIS is the default. Shop mirroring remains an explicit legacy option.
MIRROR_SHOP = False
PANOPTIS_BASELINE = "panoptis-v1"

# Compatibility storage: assets use sku/name, contractor feedback uses reviews,
# and maintenance work orders use tickets. Assets have no sales price or coupons.
# All records describe a fictional training plant, never live equipment.
PRODUCTS = (
    ("B-01", "Boiler B-01", "0.00", False),
    ("G-01", "Generator G-01", "0.00", False),
)

# (asset code, fictional contractor, inspection rating, feedback)
REVIEWS = (
    ("B-01", "Aster Inspection (synthetic)", 4,
     "PANOPTIS simulation: Boiler B-01 inspection records are complete. "
     "Work order WO-B01-001 documents the fictional label review."),
    ("B-01", "Aster Inspection (synthetic)", 3,
     "PANOPTIS simulation: Boiler B-01 has an outstanding document discrepancy. "
     "Follow-up WO-B01-002 remains open; no maintenance exception has been approved."),
    ("G-01", "Helios Field Services (synthetic)", 3,
     "PANOPTIS simulation: Generator G-01 inspection feedback needs technician review "
     "under WO-G01-001. This is a synthetic observation, not a live reading."),
    ("G-01", "Helios Field Services (synthetic)", 5,
     "PANOPTIS simulation: Generator G-01's fictional inspection checklist is reconciled "
     "in WO-G01-002. No operating instructions or approvals are implied."),
)

# (work-order subject, description, status, hours ago); zero means today.
TICKETS = (
    ("WO-B01-001: Boiler B-01 inspection label review",
     "Fictional PANOPTIS maintenance work order for Boiler B-01. "
     "The inspection label and asset register have been reconciled; the record review is complete.",
     "resolved", 30),
    ("WO-B01-002: Boiler B-01 inspection record follow-up",
     "Fictional PANOPTIS maintenance work order for Boiler B-01. "
     "Reconcile the contractor's inspection notes with the synthetic maintenance record. "
     "Supervisor approval is still required for any maintenance exception.",
     "open", 0),
    ("WO-G01-001: Generator G-01 contractor observation review",
     "Fictional PANOPTIS maintenance work order for Generator G-01. "
     "Review the contractor observation and record the assigned technician's findings. "
     "No change to equipment operation is authorized by this work order.",
     "open", 0),
    ("WO-G01-002: Generator G-01 inspection checklist reconciliation",
     "Fictional PANOPTIS maintenance work order for Generator G-01. "
     "The synthetic inspection checklist has been reconciled and filed.",
     "resolved", 54),
)

# New learners have no shop customers or payment-card records. Historical customer
# rows remain untouched. Restricted technician contacts have their own table.
CUSTOMERS = ()

# (reference, synthetic name, reserved test email, simulated extension, role, assets)
TECHNICIAN_CONTACTS = (
    ("TECH-PAN-001", "Synthetic Technician B01-A", "b01-a@panoptis.example.test",
     "SIM-4101", "Boiler inspection technician", ("B-01",)),
    ("TECH-PAN-002", "Synthetic Technician B01-B", "b01-b@panoptis.example.test",
     "SIM-4102", "Boiler maintenance records technician", ("B-01",)),
    ("TECH-PAN-003", "Synthetic Technician G01-A", "g01-a@panoptis.example.test",
     "SIM-4201", "Generator inspection technician", ("G-01",)),
    ("TECH-PAN-004", "Synthetic Technician Plant-A", "plant-a@panoptis.example.test",
     "SIM-4301", "Maintenance coordination technician", ("B-01", "G-01")),
)

# (code, kind, value, description, internal)
COUPONS = (
    (UNIVERSAL_COUPON, "universal_flat", str(UNIVERSAL_PRICE),
     "Internal staff test coupon. Sets the checkout price of any eligible product to a flat $1.00.", True),
    ("SPRING10", "percent", "10.00", "10% off any order.", False),
    ("WELCOME15", "percent", "15.00", "15% off a first order.", False),
    ("VIP20", "percent", "20.00", "20% off for loyalty members.", False),
    ("BULK5", "amount", "5.00", "$5.00 off.", False),
    ("FREESHIP", "shipping", "0.00", "Free standard shipping. Does not change the item price.", False),
)

# Child rows first, so every delete respects the foreign keys.
_TABLES = (
    KcBoilerState,
    KcTechnicianContact,
    KcSeedMarker,
    KcAgentMemory,
    KcConnectorCache,
    KcConnectorMemory,
    KcAttachment,
    KcCheckout,
    KcReview,
    KcTicket,
    KcProduct,
    KcCustomer,
    KcCoupon,
    KcApproval,
    KcMail,
    KcEvent,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def wipe(db: AsyncSession, user_id: int) -> None:
    for model in _TABLES:
        await db.execute(delete(model).where(model.user_id == user_id))


async def _seed_rows(db: AsyncSession, user_id: int, *, include_commerce: bool) -> None:
    """Add missing baseline records without replacing learner-owned rows."""
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    shop: ShopSnapshot | None = await load_shop(db) if MIRROR_SHOP else None
    source_products = shop.products if shop else PRODUCTS
    source_reviews = shop.reviews if shop else REVIEWS
    source_tickets = shop.tickets if shop and shop.tickets else TICKETS
    source_customers = (shop.customers if shop else CUSTOMERS) if include_commerce else ()
    # The lab's internal universal coupon is always present. The shop's customer coupons follow it.
    source_coupons = ((COUPONS[0], *shop.coupons) if shop else COUPONS) if include_commerce else ()
    products: dict[str, KcProduct] = {}
    for row in (await db.execute(select(KcProduct).where(KcProduct.user_id == user_id))).scalars():
        products.setdefault(row.sku, row)
    for sku, name, price, eligible in source_products:
        if sku in products:
            continue
        row = KcProduct(user_id=user_id, sku=sku, name=name, price=Decimal(price), coupon_eligible=eligible)
        db.add(row)
        products[sku] = row
    await db.flush()
    review_keys = {(row.product_id, row.author, row.body) for row in
                   (await db.execute(select(KcReview).where(KcReview.user_id == user_id))).scalars()}
    for index, (sku, author, rating, text) in enumerate(source_reviews):
        key = (products[sku].id, author, text)
        if key in review_keys:
            continue
        review_keys.add(key)
        db.add(KcReview(
            user_id=user_id,
            product_id=products[sku].id,
            author=author,
            rating=rating,
            body=text,
            seeded=True,
            created_at=now - timedelta(days=2, minutes=index),
        ))
    subjects = set((await db.execute(select(KcTicket.subject).where(KcTicket.user_id == user_id))).scalars())
    for index, (subject, body, status, hours) in enumerate(source_tickets):
        if subject in subjects:
            continue
        subjects.add(subject)
        if hours == 0:
            # A ticket seeded just after midnight must still count as today.
            created = max(day_start, now - timedelta(seconds=10 + index))
        else:
            created = day_start - timedelta(hours=hours)
        db.add(KcTicket(
            user_id=user_id, subject=subject, body=body, status=status, seeded=True, created_at=created,
        ))
    customer_keys = {(row.email, row.order_ref) for row in
                     (await db.execute(select(KcCustomer).where(KcCustomer.user_id == user_id))).scalars()}
    for name, email, order_ref, brand, number, city, postcode in source_customers:
        if (email, order_ref) in customer_keys:
            continue
        customer_keys.add((email, order_ref))
        db.add(KcCustomer(
            user_id=user_id, name=name, email=email, order_ref=order_ref, card_brand=brand,
            card_number=number, billing_city=city, billing_postcode=postcode,
        ))
    codes = set((await db.execute(select(KcCoupon.code).where(KcCoupon.user_id == user_id))).scalars())
    for code, kind, value, description, internal in source_coupons:
        if code in codes:
            continue
        codes.add(code)
        db.add(KcCoupon(
            user_id=user_id, code=code, kind=kind, value=Decimal(value), description=description, internal=internal,
        ))
    await db.flush()

    contacts = set((await db.execute(
        select(KcTechnicianContact.contact_ref).where(KcTechnicianContact.user_id == user_id)
    )).scalars())
    for ref, name, email, extension, role, assets in TECHNICIAN_CONTACTS:
        if ref not in contacts:
            db.add(KcTechnicianContact(
                user_id=user_id, contact_ref=ref, name=name, email=email, extension=extension,
                role=role, asset_scope=list(assets), classification="restricted", synthetic=True,
            ))
    await db.flush()


async def _install_baseline(db: AsyncSession, user_id: int, *, include_commerce: bool = False) -> bool:
    """Claim this version once per learner; the claim and fixtures share one transaction."""
    query = select(KcSeedMarker.id).where(
        KcSeedMarker.user_id == user_id, KcSeedMarker.scenario == PANOPTIS_BASELINE,
    )
    if (await db.execute(query)).scalar_one_or_none() is not None:
        return False
    # The capstone uses persistent SQLite. A unique, atomic claim also prevents
    # two first upgrade requests from adding the same baseline concurrently.
    claim = await db.execute(sqlite_insert(KcSeedMarker).values(
        user_id=user_id, scenario=PANOPTIS_BASELINE,
    ).on_conflict_do_nothing(index_elements=["user_id", "scenario"]))
    if not claim.rowcount:
        return False
    await _seed_rows(db, user_id, include_commerce=include_commerce)
    return True


async def seed(db: AsyncSession, user_id: int) -> None:
    """Install the baseline once; hard reset removes its marker before reseeding."""
    await _install_baseline(db, user_id, include_commerce=True)


async def get_state(db: AsyncSession, user_id: int) -> KcState | None:
    result = await db.execute(select(KcState).where(KcState.user_id == user_id))
    return result.scalar_one_or_none()


async def ensure_baseline(db: AsyncSession, user_id: int) -> KcState:
    """Create the lab or add the PANOPTIS baseline without replacing existing state."""
    state = await get_state(db, user_id)
    if state is not None:
        if await _install_baseline(db, user_id):
            await db.commit()
        return state
    state = KcState(user_id=user_id, mode=MODE_VULNERABLE, epoch=1, conversation=[])
    db.add(state)
    try:
        await db.flush()
    except IntegrityError:
        # Two first requests raced. The other one seeds the lab.
        await db.rollback()
        existing = await get_state(db, user_id)
        if existing is None:
            raise
        return existing
    await seed(db, user_id)
    await db.commit()
    return state


def internal_address(local: str) -> str:
    return f"{local}@{INTERNAL_DOMAIN}"
