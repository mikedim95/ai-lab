"""Mount the original complete kill-chain API on a fresh, session-scoped database."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import os
import re
import secrets
import sys

sys.path.insert(0, "/app")
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import uvicorn

from bridge import bridge_key, signature
from app.api.killchain import router
from app.core.database import async_session, engine, init_db
from app.core.dependencies import get_current_admin
from app.models.user import User

KEY = bridge_key(os.environ.get("DATA_DIR", "/data"))


@asynccontextmanager
async def lifespan(application):
    await init_db()
    yield
    await engine.dispose()


application = FastAPI(title="Nine Labs capstone engine", lifespan=lifespan, docs_url=None, redoc_url=None)


async def learner(request: Request):
    sid = request.headers.get("x-nine-session", "")
    provided = request.headers.get("x-nine-signature", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid) or not hmac.compare_digest(provided, signature(KEY, sid)):
        raise HTTPException(403, "Open the capstone through Nine Labs.")
    username = "learner_" + hashlib.sha256(sid.encode()).hexdigest()[:24]
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            user = User(username=username, email=username + "@example.test", password_hash="disabled:" + secrets.token_hex(32),
                        is_staff=True, is_superuser=False, is_active=True)
            db.add(user)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                user = (await db.execute(select(User).where(User.username == username))).scalar_one()
        return user


application.dependency_overrides[get_current_admin] = learner
application.include_router(router)


@application.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(application, host="0.0.0.0", port=9000, access_log=False)
