"""Shared local bridge identity. No upstream accounts or credentials are used."""
import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path


def bridge_key(directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(directory) / "nine-labs.sqlite3", timeout=15)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('flag_key', ?)", (secrets.token_hex(32),))
            return bytes.fromhex(db.execute("SELECT value FROM metadata WHERE name='flag_key'").fetchone()[0])
    finally:
        db.close()


def signature(key, sid):
    return hmac.new(key, ("nine-labs:capstone:" + sid).encode(), hashlib.sha256).hexdigest()
