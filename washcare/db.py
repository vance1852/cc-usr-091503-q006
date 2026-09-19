"""SQLite schema, connection handling and demo seed data.

设计要点：
- 授权(consents)按版本保存，家长只能在操作前缩小当前授权范围，
  缩小或撤回都会产生新版本，老版本永久保留以便事后追溯。
- 用品按批次(products/lots)管理，中心可停用整个产品或单个批次；
  已开始的洗护继续使用的批次若中途被停用，下一次更换将被拒绝。
- 会话事件(events)只追加，更正不覆盖原事件，另写 correction 事件。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS babies (
    id INTEGER PRIMARY KEY,
    baby_code TEXT NOT NULL UNIQUE,          -- 手环/脚环扫码
    name TEXT NOT NULL,
    id_tag TEXT NOT NULL,                    -- 现场人工核对的第二标识
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS caregivers (
    id INTEGER PRIMARY KEY,
    staff_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    qualification TEXT NOT NULL,             -- 所持资质名称
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,                  -- UTC ISO8601
    active INTEGER NOT NULL DEFAULT 1
);

-- 产品目录：中心可整体停用某个护肤品
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    product_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                      -- body_wash / lotion ...
    deactivated_at TEXT
);

-- 用品批次：入库来源、有效期，可单独停用（召回等）
CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    lot_code TEXT NOT NULL UNIQUE,          -- 扫码批次号
    supplier TEXT NOT NULL,
    received_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deactivated_at TEXT,
    initial_qty INTEGER NOT NULL CHECK (initial_qty >= 0),
    consumed_qty INTEGER NOT NULL DEFAULT 0 CHECK (consumed_qty >= 0)
);

CREATE TABLE IF NOT EXISTS consents (
    id INTEGER PRIMARY KEY,
    baby_id INTEGER NOT NULL REFERENCES babies(id),
    version INTEGER NOT NULL,
    scope_json TEXT NOT NULL,               -- 允许使用的 product_code 列表
    all_products INTEGER NOT NULL DEFAULT 0,-- 纸面单"默认用品"勾选项
    source TEXT NOT NULL,                   -- paper_form / verbal / app
    status TEXT NOT NULL DEFAULT 'active',  -- active / narrowed / withdrawn
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (baby_id, version)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    session_code TEXT NOT NULL UNIQUE,      -- 一次洗护的业务编号
    baby_id INTEGER NOT NULL REFERENCES babies(id),
    caregiver_id INTEGER NOT NULL REFERENCES caregivers(id),
    consent_version INTEGER NOT NULL,       -- 本次锁定的授权版本
    status TEXT NOT NULL DEFAULT 'running', -- running / paused / ended / aborted / review
    id_confirmed INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT
);

-- 只追加的事件流
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,                   -- 会话内严格递增
    client_event_id TEXT,                   -- 移动端幂等键，唯一
    type TEXT NOT NULL,                     -- start/pause/resume/product_change/
                                            -- observation/abort/end/correction/notification
    payload_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    UNIQUE (session_id, seq),
    UNIQUE (session_id, client_event_id)
);

-- 幂等消耗记录：同一 (会话, 批次, 客户端请求号) 只消耗一次
CREATE TABLE IF NOT EXISTS lot_consumptions (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    lot_id INTEGER NOT NULL REFERENCES lots(id),
    client_request_id TEXT NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    event_id INTEGER NOT NULL REFERENCES events(id),
    consumed_at TEXT NOT NULL,
    UNIQUE (session_id, lot_id, client_request_id)
);

-- 通知回执
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,                   -- sent / failed
    receipt TEXT NOT NULL DEFAULT '',
    event_id INTEGER REFERENCES events(id),
    sent_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str = ":memory:") -> sqlite3.Connection:
    # check_same_thread=False：Flask 开发服务器为每请求派线程，
    # API 层用锁串行化访问，保证跨线程共用同一连接（含内存库）的安全。
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def seed_demo_data(conn: sqlite3.Connection) -> None:
    """填入演示数据，复现题述场景：纸面单勾选默认用品，家长口头表示不放心。"""
    now = utcnow()
    conn.execute(
        "INSERT INTO babies (baby_code, name, id_tag, notes) VALUES (?,?,?,?)",
        ("BABY-0001", "李小宝", "手环 W-0001 / 床头卡 C-0001", "轻度湿疹史，耳后皮肤偏敏感"),
    )
    baby_id = conn.execute("SELECT id FROM babies WHERE baby_code='BABY-0001'").fetchone()["id"]

    conn.execute(
        "INSERT INTO caregivers (staff_code,name,qualification,valid_from,valid_to,active)"
        " VALUES (?,?,?,?,?,1)",
        ("NURSE-A07", "王育婴", "高级育婴师+婴儿皮肤护理培训", "2024-01-01T00:00:00+00:00",
         "2027-01-01T00:00:00+00:00"),
    )
    # 资质过期人员，用于核对失败的测试
    conn.execute(
        "INSERT INTO caregivers (staff_code,name,qualification,valid_from,valid_to,active)"
        " VALUES (?,?,?,?,?,1)",
        ("NURSE-X99", "赵实习", "育婴师(未复审)", "2022-01-01T00:00:00+00:00",
         "2024-12-31T00:00:00+00:00"),
    )

    products = [
        ("P-WASH", "温和婴儿沐浴露", "body_wash"),
        ("P-LOTION", "婴儿润肤乳", "lotion"),
        ("P-OIL", "婴儿按摩油", "oil"),
    ]
    for code, name, kind in products:
        conn.execute(
            "INSERT INTO products (product_code,name,kind) VALUES (?,?,?)", (code, name, kind)
        )
    rows = {r["product_code"]: r["id"] for r in conn.execute("SELECT id,product_code FROM products")}

    lots = [
        (rows["P-WASH"], "LOT-WASH-2026-09", "安护日化供应商", 10),
        (rows["P-LOTION"], "LOT-LOTION-2026-09", "安护日化供应商", 8),
        (rows["P-OIL"], "LOT-OIL-2025-03", "恒润用品供应商", 5),  # 已过期批次
    ]
    for pid, lot_code, supplier, qty in lots:
        conn.execute(
            "INSERT INTO lots (product_id,lot_code,supplier,received_at,expires_at,initial_qty)"
            " VALUES (?,?,?,?,?,?)",
            (pid, lot_code, supplier, "2026-08-01T00:00:00+00:00",
             "2027-08-01T00:00:00+00:00" if "2026" in lot_code else "2026-03-01T00:00:00+00:00",
             qty),
        )

    # 版本1：纸面服务单勾选了"默认用品"（全部产品），家长当场口头提出疑虑，备注留痕
    conn.execute(
        "INSERT INTO consents (baby_id,version,scope_json,all_products,source,status,note,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (baby_id, 1, json.dumps(["P-WASH", "P-LOTION", "P-OIL"]), 1, "paper_form", "active",
         "纸面服务单已勾选默认用品；家长口头表示对按摩油(P-OIL)不放心，待现场确认", now),
    )
    conn.commit()
