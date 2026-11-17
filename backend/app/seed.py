from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  balance_cents INTEGER NOT NULL DEFAULT 0,
  last_settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  kind TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  biz_ref TEXT NOT NULL DEFAULT '',
  target_entry_key TEXT,
  booked_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待归并',
  note TEXT NOT NULL DEFAULT '',
  settle_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS quarantine_items (
  id INTEGER PRIMARY KEY,
  entry_key TEXT NOT NULL,
  account_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  settle_run_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settle_runs (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,
  forward_count INTEGER NOT NULL DEFAULT 0,
  reversal_count INTEGER NOT NULL DEFAULT 0,
  settled_count INTEGER NOT NULL DEFAULT 0,
  quarantine_count INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_reports (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  ledger_net_cents INTEGER NOT NULL,
  balance_sum_cents INTEGER NOT NULL,
  quarantine_count INTEGER NOT NULL,
  consistent INTEGER NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- 正向键迁移表（管道内键重映射，非主数据 SCD）：一行一次 old_key→new_key 登记
-- 生效行不可变；失败行保留登记并记原因，不产生任何重指向
CREATE TABLE IF NOT EXISTS key_retargets (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER REFERENCES settle_runs(id),
  old_entry_key TEXT NOT NULL,
  new_entry_key TEXT NOT NULL,
  account_id INTEGER REFERENCES accounts(id),
  status TEXT NOT NULL,
  retargeted_reversal_count INTEGER NOT NULL DEFAULT 0,
  old_remain_cents INTEGER NOT NULL DEFAULT 0,
  new_remain_cents INTEGER NOT NULL DEFAULT 0,
  merged_remain_cents INTEGER NOT NULL DEFAULT 0,
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  applied_at TEXT
);
-- 同一旧键至多一次生效迁移；同一新键至多被一次生效迁移并入（禁止链式/合并冲突）
CREATE UNIQUE INDEX IF NOT EXISTS ux_retarget_old_active
  ON key_retargets(old_entry_key) WHERE status = '生效';
CREATE UNIQUE INDEX IF NOT EXISTS ux_retarget_new_active
  ON key_retargets(new_entry_key) WHERE status = '生效';
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        if n:
            return
        seed_path = FIXTURES_DIR / "seed.json"
        data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for a in data["accounts"]:
            conn.execute(
                "INSERT INTO accounts(id, code, name, currency, balance_cents) VALUES (?, ?, ?, ?, 0)",
                (a["id"], a["code"], a["name"], a["currency"]),
            )
        for e in data["forward_entries"]:
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '正向', ?, ?, NULL, ?, '待归并', ?)
                """,
                (
                    e["id"],
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        # 冲正 id 与正向可能重叠，用独立自增：先取 max
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM ledger_entries").fetchone()["m"]
        for i, e in enumerate(data["reversal_entries"], start=1):
            conn.execute(
                """
                INSERT INTO ledger_entries(
                  id, entry_key, account_id, kind, amount_cents, biz_ref,
                  target_entry_key, booked_at, status, note
                ) VALUES (?, ?, ?, '冲正', ?, ?, ?, ?, '待归并', ?)
                """,
                (
                    max_id + i,
                    e["entry_key"],
                    e["account_id"],
                    e["amount_cents"],
                    e.get("biz_ref", ""),
                    e.get("target_entry_key"),
                    e["booked_at"],
                    e.get("note", ""),
                ),
            )
        conn.commit()
