from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts
from app.modules.reverse_retarget import family_maps


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dashboard() -> dict:
    with connect() as conn:
        accounts = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())
        fwd = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '正向'"
        ).fetchone()["c"]
        rev = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind = '冲正'"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE status = '待归并'"
        ).fetchone()["c"]
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items").fetchone()["c"]
        bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        last = conn.execute("SELECT * FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "账户数": len(accounts),
            "正向笔数": fwd,
            "冲正笔数": rev,
            "待归并": pending,
            "未平隔离": q,
            "余额合计分": bal,
            "最近归并": dict(last) if last else None,
            "账户": accounts,
        }


def list_accounts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY id").fetchall())


def list_entries(kind: str | None = None) -> list[dict]:
    with connect() as conn:
        if kind:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                WHERE e.kind = ?
                ORDER BY e.id
                """,
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT e.*, a.code AS account_code, a.name AS account_name
                FROM ledger_entries e
                JOIN accounts a ON a.id = e.account_id
                ORDER BY e.id
                """
            ).fetchall()
        rows = rows_to_dicts(rows)
        old_to_new, _ = family_maps(conn)
        new_to_olds: dict[str, list[str]] = {}
        for o, n in old_to_new.items():
            new_to_olds.setdefault(n, []).append(o)
        # 证明同源：冲正被哪次迁移触及（迁移边 retarget_touches，只读不另造口径）
        touch_map = {
            r["reversal_key"]: dict(r)
            for r in conn.execute(
                """
                SELECT t.retarget_id, t.reversal_key, t.from_target_key,
                       t.to_target_key, t.action, p.id AS proof_id
                FROM retarget_touches t
                JOIN retarget_repoint_proofs p ON p.retarget_id = t.retarget_id
                """
            ).fetchall()
        }
        # 正向键被哪次生效迁移并入（证明 id 与迁移记录同源）
        proof_by_retarget = {
            r["retarget_id"]: r["id"]
            for r in conn.execute("SELECT id, retarget_id FROM retarget_repoint_proofs").fetchall()
        }
        retarget_rows = conn.execute(
            "SELECT id, old_entry_key, new_entry_key FROM key_retargets WHERE status = '生效'"
        ).fetchall()
        forward_proof: dict[str, dict] = {}
        for rr in retarget_rows:
            payload = {
                "迁移id": rr["id"],
                "重指向证明id": proof_by_retarget.get(rr["id"]),
            }
            forward_proof[rr["old_entry_key"]] = payload
            forward_proof.setdefault(rr["new_entry_key"], payload)
        for e in rows:
            # 迁移前后键（来源：key_retargets 生效边），页面同源可查
            e["迁移前键"] = "、".join(new_to_olds.get(e["entry_key"], []))
            e["迁移后键"] = [old_to_new[e["entry_key"]]] if e["entry_key"] in old_to_new else []
            if e["kind"] == "冲正" and e.get("target_entry_key"):
                e["目标迁移前键"] = next(
                    (o for o, n in old_to_new.items() if n == e["target_entry_key"]), ""
                )
            else:
                e["目标迁移前键"] = ""
            # 重指向证明字段：与迁移记录、流水同源（迁移边 + 证明实体，非页面文案）
            if e["kind"] == "冲正":
                touch = touch_map.get(e["entry_key"])
                e["重指向证明id"] = touch["proof_id"] if touch else None
                e["迁移id"] = touch["retarget_id"] if touch else None
                e["重指向处置"] = touch["action"] if touch else ""
                e["重指向改前键"] = touch["from_target_key"] if touch else ""
            else:
                fp = forward_proof.get(e["entry_key"])
                e["重指向证明id"] = fp["重指向证明id"] if fp else None
                e["迁移id"] = fp["迁移id"] if fp else None
        return rows


def list_quarantine() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.code AS account_code
            FROM quarantine_items q
            JOIN accounts a ON a.id = q.account_id
            ORDER BY q.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def list_settle_runs() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM settle_runs ORDER BY id DESC").fetchall()
        )


def list_verify_reports() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM verify_reports ORDER BY id DESC").fetchall()
        )


def run_settle() -> dict:
    """底座冒烟：按账户汇总正向−已匹配冲正；无原单/超额冲正进隔离；余额写账户。"""
    with connect() as conn:
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO settle_runs(status, started_at)
            VALUES ('进行中', ?)
            """,
            (now,),
        )
        run_id = cur.lastrowid

        forwards = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '正向' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        reversals = rows_to_dicts(
            conn.execute(
                "SELECT * FROM ledger_entries WHERE kind = '冲正' AND status = '待归并' ORDER BY id"
            ).fetchall()
        )
        # 迁移家族：旧键冲正重指向后，按家族（新键为头）共享正向额度
        old_to_new, members = family_maps(conn)
        all_forwards = {
            r["entry_key"]: dict(r)
            for r in conn.execute("SELECT * FROM ledger_entries WHERE kind = '正向'").fetchall()
        }

        # 每个正向家族已占用冲正额度（含历史轮已归并冲正，与 family_exposure 同源）。
        # 只统计不会在本轮重处理的已终态（已归并）冲正：待归并冲正将在下方循环中
        # 逐笔累加，预先计入会双重占用额度，把足额冲正误判成「冲正超额」隔离。
        used: dict[str, int] = {}
        for head in members:
            keys = members[head]
            placeholders = ",".join("?" for _ in keys)
            used[head] = conn.execute(
                f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger_entries "
                f"WHERE kind = '冲正' AND status = '已归并' "
                f"AND target_entry_key IN ({placeholders})",
                keys,
            ).fetchone()["s"]
        settled = 0
        quarantined = 0

        for r in reversals:
            target = r.get("target_entry_key")
            head = old_to_new.get(target, target) if target else None
            # 家族头正向（可能在历史轮已归并不在本轮 fwd_by_key，故查全量）
            fwd = all_forwards.get(head) if head else None
            if not target or fwd is None:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正无原单", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            if fwd["account_id"] != r["account_id"]:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正账户与原单不一致", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            # 家族共享正向额度：成员（新键头 + 并入旧键）正向额度合计，
            # 与 family_exposure 同源；配额按全家族算，不能只看头键单笔
            family_keys = members.get(head, [head])
            quota = sum(
                all_forwards[k]["amount_cents"] for k in family_keys if k in all_forwards
            )
            remain = quota - used.get(head, 0)
            if r["amount_cents"] > remain:
                conn.execute(
                    """
                    INSERT INTO quarantine_items(
                      entry_key, account_id, reason, amount_cents, settle_run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["entry_key"], r["account_id"], "冲正超额", r["amount_cents"], run_id, now),
                )
                conn.execute(
                    "UPDATE ledger_entries SET status = '已隔离', settle_run_id = ? WHERE id = ?",
                    (run_id, r["id"]),
                )
                quarantined += 1
                continue

            used[head] += r["amount_cents"]
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, r["id"]),
            )
            settled += 1

        for f in forwards:
            conn.execute(
                "UPDATE ledger_entries SET status = '已归并', settle_run_id = ? WHERE id = ?",
                (run_id, f["id"]),
            )
            settled += 1

        # 余额与互证同源：按全量「已归并」流水重算每个账户的净额并整表覆盖写，
        # 多轮归并后未触及账户也不会留下过期余额（隔离冲正不计入）
        net_rows = conn.execute(
            """
            SELECT account_id,
                   COALESCE(SUM(CASE WHEN kind = '正向' THEN amount_cents
                                     WHEN kind = '冲正' THEN -amount_cents END), 0) AS net
            FROM ledger_entries
            WHERE status = '已归并'
            GROUP BY account_id
            """
        ).fetchall()
        net_map = {r["account_id"]: r["net"] for r in net_rows}
        net_total = 0
        for acc in conn.execute("SELECT id FROM accounts ORDER BY id").fetchall():
            net = net_map.get(acc["id"], 0)
            conn.execute(
                "UPDATE accounts SET balance_cents = ?, last_settle_run_id = ? WHERE id = ?",
                (net, run_id, acc["id"]),
            )
            net_total += net
        finished = _now()
        conn.execute(
            """
            UPDATE settle_runs
            SET status = '完成',
                forward_count = ?,
                reversal_count = ?,
                settled_count = ?,
                quarantine_count = ?,
                net_cents = ?,
                finished_at = ?
            WHERE id = ?
            """,
            (
                len(forwards),
                len(reversals),
                settled,
                quarantined,
                net_total,
                finished,
                run_id,
            ),
        )
        conn.commit()
        return get_settle_run(run_id)


def get_settle_run(run_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "归并运行不存在")
        return dict(row)


def run_verify(settle_run_id: int | None = None) -> dict:
    with connect() as conn:
        if settle_run_id is None:
            last = conn.execute("SELECT id FROM settle_runs ORDER BY id DESC LIMIT 1").fetchone()
            if not last:
                raise HTTPException(400, "尚无归并运行，无法互证")
            settle_run_id = last["id"]
        run = conn.execute("SELECT * FROM settle_runs WHERE id = ?", (settle_run_id,)).fetchone()
        if not run:
            raise HTTPException(404, "归并运行不存在")

        # 已归并正向 − 已归并冲正
        fwd_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '正向' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        rev_sum = conn.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries
            WHERE kind = '冲正' AND status = '已归并' AND settle_run_id = ?
            """,
            (settle_run_id,),
        ).fetchone()["s"]
        ledger_net = fwd_sum - rev_sum
        balance_sum = conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts WHERE last_settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["s"]
        # 账户余额合计可能含历史；底座约定：互证看「本轮触及账户余额合计」vs 本轮流水净额
        # 简化：用全部账户余额 vs 全部已归并净额（多轮后仍应一致若每轮覆盖写）
        all_bal = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS s FROM accounts").fetchone()["s"]
        q_count = conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine_items WHERE settle_run_id = ?",
            (settle_run_id,),
        ).fetchone()["c"]

        # 全量已归并净额
        all_fwd = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '正向' AND status = '已归并'"
        ).fetchone()["s"]
        all_rev = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM ledger_entries WHERE kind = '冲正' AND status = '已归并'"
        ).fetchone()["s"]
        all_ledger_net = all_fwd - all_rev

        consistent = 1 if all_ledger_net == all_bal else 0
        detail = {
            "本轮正向分": fwd_sum,
            "本轮冲正分": rev_sum,
            "本轮流水净额分": ledger_net,
            "本轮触及余额合计分": balance_sum,
            "全量流水净额分": all_ledger_net,
            "全量账户余额分": all_bal,
            "本轮隔离数": q_count,
        }
        now = _now()
        cur = conn.execute(
            """
            INSERT INTO verify_reports(
              settle_run_id, ledger_net_cents, balance_sum_cents,
              quarantine_count, consistent, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settle_run_id,
                all_ledger_net,
                all_bal,
                q_count,
                consistent,
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM verify_reports WHERE id = ?", (rid,)).fetchone()
        out = dict(row)
        out["detail"] = detail
        return out
