PRAGMA foreign_keys = ON;

-- 婴儿（腕带码为现场扫码身份凭证）
CREATE TABLE IF NOT EXISTS babies (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    wristband_code  TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 护理人员资质
CREATE TABLE IF NOT EXISTS staff (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    qualification_no   TEXT NOT NULL UNIQUE,
    valid_until        TEXT NOT NULL,           -- ISO 日期，资质有效期截止
    active             INTEGER NOT NULL DEFAULT 1
);

-- 用品批次（中心可整批停用）
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    batch_no        TEXT NOT NULL,
    supplier        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | deactivated
    deactivated_at  TEXT,
    UNIQUE (name, batch_no)
);

-- 婴儿特殊注意事项（如过敏史等，仅做现场核对与留痕，系统不做医学判断）
CREATE TABLE IF NOT EXISTS special_notes (
    id        INTEGER PRIMARY KEY,
    baby_id   INTEGER NOT NULL REFERENCES babies(id),
    content   TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 家长授权（版本化；缩小范围产生新版本，撤回标记 status）
CREATE TABLE IF NOT EXISTS authorizations (
    id             INTEGER PRIMARY KEY,
    baby_id        INTEGER NOT NULL REFERENCES babies(id),
    version        INTEGER NOT NULL,
    supersedes_id  INTEGER REFERENCES authorizations(id),
    granted_by     TEXT NOT NULL,
    -- null / {"product_ids":[...],"actions":[...]}；null 表示全部允许
    scope_json     TEXT,
    status         TEXT NOT NULL DEFAULT 'active',  -- active | superseded | withdrawn
    withdrawn_at   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (baby_id, version)
);

-- 一次洗护操作
CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY,
    baby_id              INTEGER NOT NULL REFERENCES babies(id),
    staff_id             INTEGER NOT NULL REFERENCES staff(id),
    auth_id              INTEGER NOT NULL REFERENCES authorizations(id),
    idempotency_key      TEXT NOT NULL UNIQUE,
    auth_version         INTEGER NOT NULL,
    auth_snapshot_json   TEXT NOT NULL,   -- 开始时授权范围快照
    action               TEXT NOT NULL,
    status               TEXT NOT NULL,   -- in_progress | paused | ended | aborted
    end_reason           TEXT,
    started_at           TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at             TEXT
);

-- 按实际顺序记录的事件：start / pause / resume / product_change / observation / end
CREATE TABLE IF NOT EXISTS events (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL REFERENCES sessions(id),
    seq              INTEGER NOT NULL,
    type             TEXT NOT NULL,
    client_event_id  TEXT,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, seq),
    UNIQUE (session_id, client_event_id)
);

-- 用品消耗记录：scan_token 全局唯一，重复扫码不会二次消耗
CREATE TABLE IF NOT EXISTS product_uses (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    batch_no    TEXT NOT NULL,
    scan_token  TEXT NOT NULL UNIQUE,
    event_id    INTEGER REFERENCES events(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 异常复核（不适迹象 / 授权撤回等导致的强制终止）
CREATE TABLE IF NOT EXISTS reviews (
    id                   INTEGER PRIMARY KEY,
    session_id           INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
    reason               TEXT NOT NULL,
    observation_event_id INTEGER REFERENCES events(id),
    status               TEXT NOT NULL DEFAULT 'open',  -- open | resolved
    conclusion           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at          TEXT
);

-- 家长通知及回执
CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    review_id    INTEGER REFERENCES reviews(id),
    recipient    TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'in_app_push',
    status       TEXT NOT NULL DEFAULT 'sent',  -- sent | confirmed
    receipt_json TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    confirmed_at TEXT
);

-- 事后更正（只追加，不改动原始事件，必须注明原记录与原因）
CREATE TABLE IF NOT EXISTS corrections (
    id                INTEGER PRIMARY KEY,
    session_id        INTEGER NOT NULL REFERENCES sessions(id),
    original_event_id INTEGER NOT NULL REFERENCES events(id),
    aspect            TEXT NOT NULL,
    old_value         TEXT,
    new_value         TEXT,
    reason            TEXT NOT NULL,
    corrected_by      TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
