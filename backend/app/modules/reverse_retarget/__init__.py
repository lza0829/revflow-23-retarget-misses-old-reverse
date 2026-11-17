"""正向键迁移（管道内键重映射）与冲正重指向。

落库形态：独立键迁移表 ``key_retargets``（生效运行 id、old、new）。
原子性：登记、校验、冲正重指向、隔离释放全部在同一 SQLite 事务内完成；
超额失败则整事务回滚，冲正指向与敞口全部回到迁移前（闭失败），
只留一行 status='闭失败' 的登记记录与原因。

语义：old_key 与 new_key 是同一敞口家族的两个正向成员，迁移后
所有引用 old_key 的冲正改指向 new_key；家族正向额度 = old+new 正向
金额合计，家族剩余敞口 = 额度 − 已有效冲正累计（隔离冲正不占敞口）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# —— 同源敞口：迁移登记、迁移报告、归并配额、页面展示全部走这一处 ——

def family_maps(conn) -> tuple[dict[str, str], dict[str, list[str]]]:
    """读生效迁移，返回 old→new 映射与 new→[old...] 家族成员映射。"""
    rows = conn.execute(
        "SELECT old_entry_key, new_entry_key FROM key_retargets WHERE status = '生效'"
    ).fetchall()
    old_to_new: dict[str, str] = {r["old_entry_key"]: r["new_entry_key"] for r in rows}
    members: dict[str, list[str]] = {}
    for r in rows:
        members.setdefault(r["new_entry_key"], [r["new_entry_key"]]).append(r["old_entry_key"])
    for key in [
        r["entry_key"]
        for r in conn.execute("SELECT entry_key FROM ledger_entries WHERE kind = '正向'").fetchall()
    ]:
        members.setdefault(key, [key])
    return old_to_new, members


def family_exposure(conn, head_key: str, members: dict[str, list[str]] | None = None) -> dict:
    """单个正向家族的敞口事实（分）：正向额度、有效冲正累计、剩余敞口。

    有效冲正 = 非「已隔离」状态且 target 落在家族成员键上的冲正；
    已隔离冲正不占敞口，可在重指向后重新配平。
    """
    if members is None:
        _, members = family_maps(conn)
    keys = members.get(head_key, [head_key])
    placeholders = ",".join("?" for _ in keys)
    fwd = conn.execute(
        f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
        f"WHERE kind = '正向' AND entry_key IN ({placeholders})",
        keys,
    ).fetchone()["s"]
    used = conn.execute(
        f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
        f"WHERE kind = '冲正' AND status != '已隔离' AND target_entry_key IN ({placeholders})",
        keys,
    ).fetchone()["s"]
    return {
        "head_key": head_key,
        "member_keys": keys,
        "forward_cents": fwd,
        "reversed_cents": used,
        "remain_cents": fwd - used,
    }


def _record_failure(conn, old_key: str, new_key: str, account_id: int | None, reason: str) -> dict:
    """闭失败登记：单独事务落一行失败记录，不动任何流水/隔离/余额。"""
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO key_retargets(
          old_entry_key, new_entry_key, account_id, status, fail_reason, created_at
        ) VALUES (?, ?, ?, '闭失败', ?, ?)
        """,
        (old_key, new_key, account_id, reason, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM key_retargets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def register_retarget(old_key: str, new_key: str) -> dict:
    """登记一次正向键迁移（old_key→new_key，同账户）。

    成功：事务内写入生效迁移、重指向全部引用 old_key 的冲正、
    释放这些冲正此前的隔离项并复位为「待归并」。
    失败：全部回到迁移前，返回闭失败登记（HTTP 400）。
    """
    if not old_key or not new_key or old_key == new_key:
        raise HTTPException(400, "old_key 与 new_key 不能为空且不能相同")

    conn = connect()
    try:
        old_fwd = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_key = ? AND kind = '正向'",
            (old_key,),
        ).fetchone()
        new_fwd = conn.execute(
            "SELECT * FROM ledger_entries WHERE entry_key = ? AND kind = '正向'",
            (new_key,),
        ).fetchone()

        if old_fwd is None:
            raise _Fail(_record_failure(conn, old_key, new_key, None, "旧键正向流水不存在"))
        if new_fwd is None:
            raise _Fail(_record_failure(
                conn, old_key, new_key, old_fwd["account_id"], "新键正向流水不存在"))
        if old_fwd["account_id"] != new_fwd["account_id"]:
            raise _Fail(_record_failure(
                conn, old_key, new_key, old_fwd["account_id"], "新旧键不属于同一账户，禁止跨账户迁移"))

        account_id = old_fwd["account_id"]
        dup_old = conn.execute(
            "SELECT 1 FROM key_retargets WHERE old_entry_key = ? AND status = '生效'",
            (old_key,),
        ).fetchone()
        if dup_old:
            raise _Fail(_record_failure(conn, old_key, new_key, account_id, "旧键已存在生效迁移，禁止重复迁移"))
        dup_new = conn.execute(
            "SELECT 1 FROM key_retargets WHERE new_entry_key = ? AND status = '生效'",
            (new_key,),
        ).fetchone()
        if dup_new:
            raise _Fail(_record_failure(conn, old_key, new_key, account_id, "新键已是迁移目标，禁止链式迁移"))

        # 迁移前敞口快照（同源 family_exposure）
        _, members_before = family_maps(conn)
        old_exp = family_exposure(conn, old_key, members_before)
        new_exp = family_exposure(conn, new_key, members_before)

        now = _now()
        run_cur = conn.execute(
            "INSERT INTO settle_runs(status, started_at, finished_at) VALUES ('键迁移', ?, ?)",
            (now, now),
        )
        run_id = run_cur.lastrowid

        try:
            # 1) 重指向：所有仍引用 old_key 的冲正（含已隔离）一律改指 new_key
            rev_rows = conn.execute(
                "SELECT id, entry_key, status FROM ledger_entries "
                "WHERE kind = '冲正' AND target_entry_key = ? ORDER BY id",
                (old_key,),
            ).fetchall()
            conn.execute(
                "UPDATE ledger_entries SET target_entry_key = ? "
                "WHERE kind = '冲正' AND target_entry_key = ?",
                (new_key, old_key),
            )

            # 2) 释放被重指向冲正的旧隔离项，复位为待归并，交由下轮归并重新配平
            for r in rev_rows:
                if r["status"] == "已隔离":
                    conn.execute(
                        "DELETE FROM quarantine_items WHERE entry_key = ?",
                        (r["entry_key"],),
                    )
                    conn.execute(
                        "UPDATE ledger_entries SET status = '待归并', settle_run_id = NULL WHERE id = ?",
                        (r["id"],),
                    )

            # 3) 合并后超额闭失败：家族有效冲正累计不得超过家族正向额度
            merged_members = {new_key: list(dict.fromkeys([new_key, old_key]))}
            merged = family_exposure(conn, new_key, merged_members)
            if merged["reversed_cents"] > merged["forward_cents"]:
                reason = (
                    f"合并后冲正累计 {merged['reversed_cents']} 分超过家族正向额度 "
                    f"{merged['forward_cents']} 分（新键已有独立冲正致超额）"
                )
                conn.execute(
                    """
                    INSERT INTO key_retargets(
                      settle_run_id, old_entry_key, new_entry_key, account_id, status,
                      retargeted_reversal_count, old_remain_cents, new_remain_cents,
                      merged_remain_cents, created_at, applied_at
                    ) VALUES (?, ?, ?, ?, '生效', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, old_key, new_key, account_id,
                        len(rev_rows), old_exp["remain_cents"], new_exp["remain_cents"],
                        merged["remain_cents"], now, now,
                    ),
                )
                conn.commit()
                raise _Fail(_record_failure(conn, old_key, new_key, account_id, reason))

            # 4) 生效迁移落表（不可变边快照）
            conn.execute(
                """
                INSERT INTO key_retargets(
                  settle_run_id, old_entry_key, new_entry_key, account_id, status,
                  retargeted_reversal_count, old_remain_cents, new_remain_cents,
                  merged_remain_cents, created_at, applied_at
                ) VALUES (?, ?, ?, ?, '生效', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, old_key, new_key, account_id,
                    len(rev_rows), old_exp["remain_cents"], new_exp["remain_cents"],
                    merged["remain_cents"], now, now,
                ),
            )
            conn.commit()
        except _Fail:
            raise
        except Exception as exc:
            conn.commit()
            raise _Fail(_record_failure(conn, old_key, new_key, account_id, f"事务异常：{exc}"))

        row = conn.execute(
            "SELECT * FROM key_retargets WHERE settle_run_id = ? AND status = '生效'",
            (run_id,),
        ).fetchone()
        return dict(row)
    except _Fail as fail:
        raise HTTPException(status_code=400, detail=fail.payload)
    finally:
        conn.close()


class _Fail(Exception):
    def __init__(self, payload: dict):
        self.payload = payload


def list_retargets() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM key_retargets ORDER BY id DESC"
        ).fetchall()
        out = rows_to_dicts(rows)
        # 同源补注：生效迁移的家族当前敞口（迁移表 → 冲正 target → 敞口一致）
        _, members = family_maps(conn)
        for r in out:
            if r["status"] == "生效":
                r["当前家族敞口分"] = family_exposure(
                    conn, r["new_entry_key"], members
                )["remain_cents"]
            else:
                r["当前家族敞口分"] = None
        return out
