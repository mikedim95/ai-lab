"""Persistent capstone-only B-01 arithmetic simulator. No device or network I/O."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.labs.killchain.trace import emit, iso
from app.models.killchain import KcBoilerState, KcState

TOOL_NAME = "set_simulated_boiler_inputs"
EVENT_KIND = "boiler_inputs_updated"
MIN_INPUT = 45
MAX_INPUT = 75


def validate_arguments(arguments: dict[str, Any] | None) -> dict[str, float]:
    if not isinstance(arguments, dict) or set(arguments) != {"water_in_pct", "fuel_in_pct"}:
        raise ValueError("Exactly water_in_pct and fuel_in_pct are required.")
    result = {}
    for field in ("water_in_pct", "fuel_in_pct"):
        value = arguments[field]
        # Comparisons reject NaN and infinities too. Do not coerce strings or booleans.
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not MIN_INPUT <= value <= MAX_INPUT:
            raise ValueError(f"{field} must be a finite number between 45 and 75 inclusive.")
        result[field] = float(value)
    return result


def power_output(water_in_pct: float, fuel_in_pct: float) -> Decimal:
    """Synthetic kW = water * fuel / 100, with decimal half-up rounding to 0.01 kW."""
    return (Decimal(str(water_in_pct)) * Decimal(str(fuel_in_pct)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def view(row: KcBoilerState) -> dict[str, Any]:
    return {
        "asset": "B-01", "scope": "capstone", "simulated": True,
        "water_in_pct": row.water_in_pct, "fuel_in_pct": row.fuel_in_pct,
        "power_output_kw": float(row.power_output_kw), "revision": row.revision,
        "updated_at": iso(row.updated_at),
    }


async def get_state(db: AsyncSession, user_id: int) -> KcBoilerState:
    query = select(KcBoilerState).where(KcBoilerState.user_id == user_id).execution_options(populate_existing=True)
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        # Additive initialization also covers learners with an existing baseline marker.
        await db.execute(insert(KcBoilerState).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"]))
        await db.commit()
        row = (await db.execute(query)).scalar_one()
    return row


async def set_inputs(ctx: Any, water_in_pct: float, fuel_in_pct: float) -> dict[str, Any]:
    from app.labs.killchain import policy

    try:
        arguments = validate_arguments({"water_in_pct": water_in_pct, "fuel_in_pct": fuel_in_pct})
    except ValueError as exc:
        return {"error": "invalid_arguments", "message": str(exc)}
    try:
        blocked = await policy.guard(ctx, TOOL_NAME, arguments)
        if blocked:
            return blocked
        before = view(await get_state(ctx.db, ctx.user_id))
        timestamp = datetime.now(timezone.utc)
        output = power_output(**arguments)
        result = await ctx.db.execute(
            update(KcBoilerState).where(
                KcBoilerState.user_id == ctx.user_id,
                KcBoilerState.revision == before["revision"],
                select(KcState.id).where(KcState.user_id == ctx.user_id, KcState.epoch == ctx.epoch).exists(),
            ).values(**arguments, power_output_kw=output, revision=before["revision"] + 1, updated_at=timestamp)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await ctx.db.rollback()
            return {"error": "state_conflict", "message": "The capstone state changed. Read the current state and retry."}
        after = {
            **before, **arguments, "power_output_kw": float(output),
            "revision": before["revision"] + 1, "updated_at": iso(timestamp),
        }
        # emit commits both the state update and its before/after event in one transaction.
        event = await emit(
            ctx.db, ctx.user_id, ctx.op_id, EVENT_KIND,
            f"Simulated capstone Boiler B-01 inputs updated (revision {after['revision']})",
            detail={"tool": TOOL_NAME, "arguments": arguments, "before": before, "after": after,
                    "simulated": True, "network": "none"},
            refs={"approval_id": ctx.token.approval_id if ctx.token else None},
        )
        return {"updated": True, "before": before, "after": after, "event_id": event.id}
    except OperationalError as exc:
        await ctx.db.rollback()
        # SQLite BUSY/LOCKED (including BUSY_SNAPSHOT) are conflicting writers, not successes.
        if (getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF) in {5, 6}:
            return {"error": "state_conflict", "message": "The capstone state changed. Read the current state and retry."}
        return {"error": "state_update_failed", "message": "The simulated update was rolled back."}
    except SQLAlchemyError:
        await ctx.db.rollback()
        return {"error": "state_update_failed", "message": "The simulated update was rolled back."}
