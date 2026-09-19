"""SQLite 连接与初始化。"""
import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db: sqlite3.Connection | None = None) -> None:
    own = db is None
    if own:
        db = sqlite3.connect(current_app.config["DATABASE"])
        db.execute("PRAGMA foreign_keys = ON")
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        db.executescript(f.read())
    if own:
        db.commit()
        db.close()
