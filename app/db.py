import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_tx(db_path: str | None = None):
    """写事务：BEGIN IMMEDIATE 先拿写锁，读-判-写整段串行，避免并发双写。"""
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  gate_voucher TEXT NOT NULL DEFAULT '',
  gate_decision TEXT NOT NULL DEFAULT '',
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  gate_voucher TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  lineage_summary TEXT NOT NULL DEFAULT '',
  lineage_aligned INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 子表列 -> 父表列的血缘边。判定只按父表现行契约。
CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  child_table TEXT NOT NULL,
  child_field TEXT NOT NULL,
  parent_table TEXT NOT NULL,
  parent_field TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(child_table, child_field, parent_table, parent_field)
);

-- 打开子表升版卡片时冻结的缺边快照（本项目只采用这一种齐套门槛）。
CREATE TABLE IF NOT EXISTS lineage_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  child_table TEXT NOT NULL,
  digest TEXT NOT NULL,
  parent_state TEXT NOT NULL,
  missing_edges TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('齐套','缺边')),
  status TEXT NOT NULL DEFAULT '冻结' CHECK (status IN ('冻结','已裁决','已作废')),
  voucher TEXT NOT NULL DEFAULT '',
  frozen_by TEXT NOT NULL DEFAULT '',
  frozen_at TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshot_frozen
  ON lineage_snapshots(contract_id) WHERE status='冻结';

-- 一次齐套动作只落一条裁决，UNIQUE 兜住并发双写。
CREATE TABLE IF NOT EXISTS lineage_gates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id INTEGER NOT NULL UNIQUE REFERENCES lineage_snapshots(id),
  contract_id INTEGER NOT NULL,
  child_table TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('齐套放行','隔离')),
  voucher TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 进度水线：冻结 / 齐套放行 / 隔离 / 升版 各亮一次凭证号。
CREATE TABLE IF NOT EXISTS lineage_watermarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  child_table TEXT NOT NULL,
  stage TEXT NOT NULL,
  voucher TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
    (
        "dim_customer_d",
        1,
        "停用",
        "初版（后删 cust_level、channel_code）",
        [("cust_id", "INT"), ("cust_level", "STRING"), ("channel_code", "STRING"), ("name", "STRING")],
    ),
    (
        "dim_customer_d",
        2,
        "现行",
        "删 cust_level、channel_code，待子表重接血缘",
        [("cust_id", "INT"), ("name", "STRING")],
    ),
    (
        "dw_payments_d",
        2,
        "草稿",
        "挂客户维度并补渠道列",
        [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP"), ("cust_id", "INT"), ("pay_channel", "STRING")],
    ),
    (
        "dw_refunds_d",
        1,
        "草稿",
        "退款挂支付",
        [("refund_id", "INT"), ("pay_id", "INT"), ("refund_at", "TIMESTAMP")],
    ),
]

SEED_EDGES = [
    ("dw_payments_d", "cust_id", "dim_customer_d", "cust_id", "客户主键"),
    ("dw_payments_d", "pay_channel", "dim_customer_d", "channel_code", "渠道编码（父表已删）"),
    ("dw_payments_d", "order_id", "dw_orders_d", "order_id", "订单主键"),
    ("dw_refunds_d", "pay_id", "dw_payments_d", "pay_id", "支付主键"),
]

SEED_VERSION = "2"

MIGRATIONS = [
    ("apply_runs", "gate_voucher", "TEXT NOT NULL DEFAULT ''"),
    ("catalog_tables", "gate_voucher", "TEXT NOT NULL DEFAULT ''"),
    ("catalog_tables", "gate_decision", "TEXT NOT NULL DEFAULT ''"),
    ("reconcile_reports", "lineage_summary", "TEXT NOT NULL DEFAULT ''"),
    ("reconcile_reports", "lineage_aligned", "INTEGER NOT NULL DEFAULT 1"),
]


def _migrate(conn) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # 种子只在“该版本还没种过”时补一次：老库能补到新增的演示契约与血缘边，
        # 但用户改过的边（含撤边）不会在下次重启被种子盖回来。
        seeded = conn.execute(
            "SELECT value FROM meta WHERE key='seed_version'"
        ).fetchone()
        if seeded and seeded["value"] == SEED_VERSION:
            return
        for table, ver, status, note, fields in SEED:
            conn.execute(
                "INSERT OR IGNORE INTO schema_contracts(table_name, version_no, status, note) "
                "VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            row = conn.execute(
                "SELECT id FROM schema_contracts WHERE table_name=? AND version_no=?",
                (table, ver),
            ).fetchone()
            cid = row["id"]
            for fname, ftype in fields:
                conn.execute(
                    "INSERT OR IGNORE INTO contract_fields(contract_id, field_name, field_type) "
                    "VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                # 只在目录还没有该表时种入：重启不得把已升到新版的现行版本改回去。
                conn.execute(
                    "INSERT OR IGNORE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        for child, cfield, parent, pfield, note in SEED_EDGES:
            conn.execute(
                "INSERT OR IGNORE INTO lineage_edges(child_table, child_field, parent_table, parent_field, note) "
                "VALUES (?,?,?,?,?)",
                (child, cfield, parent, pfield, note),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('seed_version', ?)",
            (SEED_VERSION,),
        )
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('seeded','1')")
