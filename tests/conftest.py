import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from carelog import create_app
from carelog.db import init_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db_path=path)
    with app.app_context():
        init_db()
    app.config["TESTING"] = True
    yield app
    os.unlink(path)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seed(app):
    """一套典型现场数据：婴儿+腕带、合格人员、两个用品批次、注意事项。"""
    with app.app_context():
        from carelog.db import get_db
        db = get_db()
        cur = db.execute("INSERT INTO babies (name, wristband_code) VALUES (?,?)",
                         ("小宝", "WB-001"))
        baby_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO staff (name, qualification_no, valid_until) VALUES (?,?,?)",
            ("育婴师李", "NURSE-2026-01", "2030-01-01"))
        staff_id = cur.lastrowid
        cur = db.execute(
            "INSERT INTO products (name, batch_no, supplier) VALUES (?,?,?)",
            ("婴儿润肤乳", "B20260901", "安护用品厂"))
        p1 = cur.lastrowid
        cur = db.execute(
            "INSERT INTO products (name, batch_no, supplier) VALUES (?,?,?)",
            ("婴儿沐浴露", "B20260902", "安护用品厂"))
        p2 = cur.lastrowid
        db.execute("INSERT INTO special_notes (baby_id, content) VALUES (?,?)",
                   (baby_id, "面颊轻度湿疹，避免香精类产品"))
        db.commit()
        return {"baby_id": baby_id, "staff_id": staff_id, "p1": p1, "p2": p2}
