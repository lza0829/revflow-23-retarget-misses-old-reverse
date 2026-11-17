"""正向键迁移（管道内键重映射）与冲正重指向。

落库形态（迁移关系，择一实现中的「独立迁移表」）：
  * ``key_retargets``          —— 迁移登记（old_key、new_key、状态、敞口快照）；
  * ``retarget_touches``       —— 不可变迁移边快照：迁移触及的每个冲正一行
                                  （旧键、新键、触及冲正键、处置、改前/改后 target）；
  * ``retarget_repoint_proofs`` —— 重指向证明实体（旧键、新键、重指向数、未触及键）。

重指向路径（择一实现中的「同事务整笔回滚」路径）：
  在**同一个 SQLite 事务**内完成校验 → 未终态冲正 target 改指新键并同步隔离/
  敞口键 → 落迁移边与重指向证明；合并后家族有效冲正累计超过正向额度，或事务内
  任何一步异常，一律 ``ROLLBACK`` 回到迁移前——**禁止留下半截 target**。
  回滚后另起事务只落一行 status='闭失败' 的登记记录与原因，不动任何流水。

终态语义：
  * 未终态冲正（待归并/已隔离）：target 必须重指向新键；已隔离项同步释放隔离、
    复位待归并，敞口键随 target 一并改指新键。
  * 已终态冲正（已归并）：历史配平事实不可改写，target 留在旧键，但在
    ``retarget_touches`` 留下可查迁移边（action='终态留边'），二次归并经
    ``family_maps`` 的 old→new 映射仍能按家族找到原单，不会进假隔离。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts

# 冲正终态：已归并即历史配平事实，不可再改 target
TERMINAL_STATUS = "已归并"


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

    有效冲正 = 非「已隔离」状态且 target 落在家族成员键上的冲正（含终态留边、
    target 仍指旧键的已归并冲正——家族成员键覆盖旧键）；
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
    """闭失败登记：独立事务落一行失败记录，不动任何流水/隔离/余额/证明。"""
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


class _Fail(Exception):
    def __init__(self, payload: dict):
        self.payload = payload


def register_retarget(old_key: str, new_key: str) -> dict:
    """登记一次正向键迁移（old_key→new_key，同账户）。

    成功（单事务原子提交）：
      1. 未终态冲正（待归并/已隔离）target 全部重指向 new_key，已隔离项释放隔离、
         复位待归并——敞口键随 target 同步改指；
      2. 已终态冲正（已归并）target 不动，落「终态留边」迁移边；
      3. 写生效迁移、逐冲正迁移边、重指向证明（重指向数/触及键/未触及键）。
    失败：整事务 ROLLBACK，冲正指向、隔离、敞口全部回到迁移前，另起事务只留
      一行闭失败登记（HTTP 400），无任何半截 target、无证明、无迁移边。
    """
    if not old_key or not new_key or old_key == new_key:
        raise HTTPException(400, "old_key 与 new_key 不能为空且不能相同")

    conn = connect()
    try:
        # —— 迁移前校验（只读，失败直接闭失败登记，尚无任何写入）——
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
        # 显式事务模式：sqlite3 旧版 isolation_level 会在 DML 前隐式开事务，
        # 使手写 BEGIN/ROLLBACK 失效（失败后留下半截 settle_runs/target）。
        # autocommit 下 BEGIN IMMEDIATE … COMMIT/ROLLBACK 的边界完全由本函数掌控。
        conn.isolation_level = None
        # —— 原子事务：迁移边、重指向、隔离释放、证明要么全成，要么整笔回滚 ——
        conn.execute("BEGIN IMMEDIATE")
        try:
            run_cur = conn.execute(
                "INSERT INTO settle_runs(status, started_at, finished_at) VALUES ('键迁移', ?, ?)",
                (now, now),
            )
            run_id = run_cur.lastrowid

            # 1) 触及集：所有仍引用 old_key 的冲正，按终态分流
            rev_rows = conn.execute(
                "SELECT id, entry_key, status FROM ledger_entries "
                "WHERE kind = '冲正' AND target_entry_key = ? ORDER BY id",
                (old_key,),
            ).fetchall()
            repoint_rows = [r for r in rev_rows if r["status"] != TERMINAL_STATUS]
            terminal_rows = [r for r in rev_rows if r["status"] == TERMINAL_STATUS]

            # 未触及键快照必须在重指向 UPDATE 之前取：同账户其余冲正
            # （target 不指旧键或无原单），证明本次重指向范围恰好覆盖触及集
            untouched = conn.execute(
                """
                SELECT entry_key, target_entry_key FROM ledger_entries
                WHERE kind = '冲正' AND account_id = ?
                  AND (target_entry_key IS NULL OR target_entry_key != ?)
                ORDER BY id
                """,
                (account_id, old_key),
            ).fetchall()
            untouched_keys = [
                {"entry_key": r["entry_key"], "target_entry_key": r["target_entry_key"]}
                for r in untouched
            ]

            # 2) 未终态冲正：同事务改 target 并同步敞口键（含已隔离项释放）
            conn.execute(
                "UPDATE ledger_entries SET target_entry_key = ? "
                "WHERE kind = '冲正' AND target_entry_key = ? AND status != ?",
                (new_key, old_key, TERMINAL_STATUS),
            )
            for r in repoint_rows:
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
                # 整笔回滚到迁移前：target、隔离、敞口、迁移边、证明全部不落
                conn.rollback()
                raise _Fail(_record_failure(conn, old_key, new_key, account_id, reason))

            # 4) 生效迁移落表（独立迁移表，不可变边快照）
            ret_cur = conn.execute(
                """
                INSERT INTO key_retargets(
                  settle_run_id, old_entry_key, new_entry_key, account_id, status,
                  retargeted_reversal_count, old_remain_cents, new_remain_cents,
                  merged_remain_cents, created_at, applied_at
                ) VALUES (?, ?, ?, ?, '生效', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, old_key, new_key, account_id,
                    len(repoint_rows), old_exp["remain_cents"], new_exp["remain_cents"],
                    merged["remain_cents"], now, now,
                ),
            )
            retarget_id = ret_cur.lastrowid

            # 5) 逐冲正不可变迁移边：未终态=重指向；已终态=终态留边（target 不改）
            for r in repoint_rows:
                conn.execute(
                    """
                    INSERT INTO retarget_touches(
                      retarget_id, reversal_key, from_target_key, to_target_key,
                      reversal_status, action, created_at
                    ) VALUES (?, ?, ?, ?, ?, '重指向', ?)
                    """,
                    (retarget_id, r["entry_key"], old_key, new_key, r["status"], now),
                )
            for r in terminal_rows:
                conn.execute(
                    """
                    INSERT INTO retarget_touches(
                      retarget_id, reversal_key, from_target_key, to_target_key,
                      reversal_status, action, created_at
                    ) VALUES (?, ?, ?, ?, ?, '终态留边', ?)
                    """,
                    (retarget_id, r["entry_key"], old_key, old_key, r["status"], now),
                )

            # 6) 重指向证明（未触及键快照取自步骤 1，重指向 UPDATE 之前）
            touched_keys = [r["entry_key"] for r in rev_rows]
            conn.execute(
                """
                INSERT INTO retarget_repoint_proofs(
                  retarget_id, settle_run_id, old_entry_key, new_entry_key,
                  repointed_count, terminal_edge_count,
                  touched_keys_json, untouched_keys_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    retarget_id, run_id, old_key, new_key,
                    len(repoint_rows), len(terminal_rows),
                    json.dumps(touched_keys, ensure_ascii=False),
                    json.dumps(untouched_keys, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        except _Fail:
            raise
        except Exception as exc:
            # 任何中途异常：整笔回滚，禁止半截 target，再闭失败登记
            conn.rollback()
            raise _Fail(_record_failure(
                conn, old_key, new_key, account_id, f"事务异常已整笔回滚：{exc}"))

        row = conn.execute(
            "SELECT * FROM key_retargets WHERE id = ?",
            (retarget_id,),
        ).fetchone()
        return dict(row)
    except _Fail as fail:
        raise HTTPException(status_code=400, detail=fail.payload)
    finally:
        conn.close()


def get_repoint_proof(retarget_id: int) -> dict:
    """只读接口：按迁移 id 查询重指向证明（含迁移边与未触及键）。

    闭失败/不存在的迁移没有证明（事务已回滚，证明实体从未落库）→ 404。
    """
    with connect() as conn:
        retarget = conn.execute(
            "SELECT * FROM key_retargets WHERE id = ?", (retarget_id,)
        ).fetchone()
        if retarget is None:
            raise HTTPException(404, f"迁移 {retarget_id} 不存在")
        proof = conn.execute(
            "SELECT * FROM retarget_repoint_proofs WHERE retarget_id = ?",
            (retarget_id,),
        ).fetchone()
        if proof is None:
            raise HTTPException(
                404,
                f"迁移 {retarget_id} 状态为 {retarget['status']}，"
                "未产生重指向证明（闭失败迁移整笔回滚，无证明实体）",
            )
        touches = conn.execute(
            """
            SELECT reversal_key, from_target_key, to_target_key,
                   reversal_status, action, created_at
            FROM retarget_touches WHERE retarget_id = ? ORDER BY id
            """,
            (retarget_id,),
        ).fetchall()
        out = dict(proof)
        out["touched_keys"] = json.loads(out.pop("touched_keys_json"))
        out["untouched_keys"] = json.loads(out.pop("untouched_keys_json"))
        out["迁移登记"] = dict(retarget)
        out["迁移边"] = rows_to_dicts(touches)
        return out


def list_retargets() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM key_retargets ORDER BY id DESC"
        ).fetchall()
        out = rows_to_dicts(rows)
        # 同源补注：生效迁移的家族当前敞口（迁移表 → 冲正 target → 敞口一致）
        _, members = family_maps(conn)
        proof_ids = {
            r["retarget_id"]
            for r in conn.execute("SELECT retarget_id FROM retarget_repoint_proofs").fetchall()
        }
        for r in out:
            if r["status"] == "生效":
                r["当前家族敞口分"] = family_exposure(
                    conn, r["new_entry_key"], members
                )["remain_cents"]
                # 证明同源：列表只给证明 id，证明全文走只读接口按迁移 id 查
                r["重指向证明id"] = r["id"] if r["id"] in proof_ids else None
            else:
                r["当前家族敞口分"] = None
                r["重指向证明id"] = None
        return out
