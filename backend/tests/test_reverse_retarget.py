"""reverse_retarget：正向键迁移、冲正重指向、终态留边、闭失败整笔回滚、重指向证明、同源互证。"""
from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# 独立测试库，避免与冒烟测试共享 DATA_DIR
TEST_DIR = ROOT / "backend" / "data_test_retarget"
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

from app.main import app  # noqa: E402
from app.db import connect  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def isolated_db():
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    db_file = TEST_DIR / "app.db"
    if db_file.exists():
        db_file.unlink()
    prev = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(TEST_DIR)
    yield
    if prev is None:
        os.environ.pop("DATA_DIR", None)
    else:
        os.environ["DATA_DIR"] = prev


def _insert_forward(key: str, account_id: int, amount: int, booked: str = "2026-11-17T08:00:00Z"):
    with connect() as conn:
        conn.execute(
            """INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents,
               biz_ref, booked_at, status) VALUES (?, ?, '正向', ?, 'REPLAY', ?, '待归并')""",
            (key, account_id, amount, booked),
        )
        conn.commit()


def _insert_reversal(key: str, account_id: int, amount: int, target: str | None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents,
               biz_ref, target_entry_key, booked_at, status)
               VALUES (?, ?, '冲正', ?, 'REPLAY', ?, '2026-11-17T09:00:00Z', '待归并')""",
            (key, account_id, amount, target),
        )
        conn.commit()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_retarget_repoints_open_reversals_and_merges_exposure(client):
    # 装载重放：FWD-...-002 换键为 NEW-002，原部分冲正 REV-...-002（待归并，未终态）仍指旧键
    _insert_forward("NEW-20261117-002", 1, 25000)

    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-002", "new_key": "NEW-20261117-002"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "生效"
    assert body["retargeted_reversal_count"] == 1  # REV-...-002 被重指向
    # 旧键剩余 25000-5000=20000；新键原剩余 25000；合并剩余 45000（额度 50000 − 已冲 5000）
    assert body["old_remain_cents"] == 20000
    assert body["new_remain_cents"] == 25000
    assert body["merged_remain_cents"] == 45000
    assert body["settle_run_id"] is not None

    # 禁止只改正向行主键却留下冲正悬空：冲正 target 必须已改指新键
    with connect() as conn:
        rev = conn.execute(
            "SELECT target_entry_key FROM ledger_entries WHERE entry_key = 'REV-20261116-002'"
        ).fetchone()
        assert rev["target_entry_key"] == "NEW-20261117-002"
        dangling = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entries WHERE kind='冲正' AND target_entry_key = 'FWD-20261116-002'"
        ).fetchone()["c"]
        assert dangling == 0
        # 迁移表可查
        m = conn.execute("SELECT * FROM key_retargets WHERE status='生效' AND old_entry_key = 'FWD-20261116-002'").fetchone()
        assert m["new_entry_key"] == "NEW-20261117-002"

    # 重指向证明实体：按迁移 id 只读查询，字段齐全且与迁移记录、流水同源
    proof = client.get(f"/api/key-retargets/{body['id']}/repoint-proof").json()
    assert proof["old_entry_key"] == "FWD-20261116-002"
    assert proof["new_entry_key"] == "NEW-20261117-002"
    assert proof["repointed_count"] == 1
    assert proof["terminal_edge_count"] == 0
    assert proof["touched_keys"] == ["REV-20261116-002"]
    # 未触及键：同账户其余冲正（REV-001 指 001、REV-004 无原单），证明重指向范围恰好覆盖
    untouched_keys = [x["entry_key"] for x in proof["untouched_keys"]]
    assert "REV-20261116-001" in untouched_keys
    assert "REV-20261116-004" in untouched_keys
    assert "REV-20261116-002" not in untouched_keys
    assert proof["迁移登记"]["id"] == body["id"]
    edge = proof["迁移边"][0]
    assert edge["reversal_key"] == "REV-20261116-002"
    assert edge["action"] == "重指向"
    assert edge["from_target_key"] == "FWD-20261116-002"
    assert edge["to_target_key"] == "NEW-20261117-002"

    # 页面同源：正向/冲正页都能看到迁移前后键与证明字段
    fwd_rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=正向").json()}
    assert fwd_rows["FWD-20261116-002"]["迁移后键"] == ["NEW-20261117-002"]
    assert fwd_rows["NEW-20261117-002"]["迁移前键"] == "FWD-20261116-002"
    assert fwd_rows["FWD-20261116-002"]["重指向证明id"] == proof["id"]
    rev_rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=冲正").json()}
    assert rev_rows["REV-20261116-002"]["target_entry_key"] == "NEW-20261117-002"
    assert rev_rows["REV-20261116-002"]["目标迁移前键"] == "FWD-20261116-002"
    assert rev_rows["REV-20261116-002"]["重指向证明id"] == proof["id"]
    assert rev_rows["REV-20261116-002"]["重指向处置"] == "重指向"
    # 未触及冲正不带证明字段
    assert rev_rows["REV-20261116-001"]["重指向证明id"] is None

    # 归并：家族共享额度，重指向冲正正常配平，不产生「有冲正无原单」假隔离
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    q = client.get("/api/quarantine").json()
    assert not any(x["entry_key"] == "REV-20261116-002" for x in q)
    rev_after = {e["entry_key"]: e for e in client.get("/api/entries?kind=冲正").json()}
    assert rev_after["REV-20261116-002"]["status"] == "已归并"

    # 互证同源：流水净额 == 余额合计
    rep = client.post("/api/verify-reports").json()
    assert rep["consistent"] == 1
    assert rep["ledger_net_cents"] == rep["balance_sum_cents"]


def test_retarget_terminal_reversal_keeps_edge_and_repairs_quarantine(client):
    # 重放把 003 换键；冲正 REV-...-003 已在上轮归并（已终态），target 必须留在旧键，
    # 但留下可查迁移边；二次归并按家族找得到原单，不进假隔离。
    # 先在换键前跑一轮归并，让 REV-...-003 成为已终态（已归并）冲正
    pre = client.post("/api/settle-runs").json()
    assert pre["status"] == "完成"
    with connect() as conn:
        st = conn.execute(
            "SELECT status FROM ledger_entries WHERE entry_key='REV-20261116-003'"
        ).fetchone()["status"]
        assert st == "已归并"
    # 装载重放：003 换键为 NEW-003
    _insert_forward("NEW-20261117-003", 2, 18000)
    # 再造一笔此前因换键被隔离（无原单）的未终态冲正，引用旧键
    _insert_reversal("REV-REPAIR-001", 2, 3000, "FWD-20261116-003")
    with connect() as conn:
        run_id = conn.execute("INSERT INTO settle_runs(status, started_at) VALUES ('完成', '2026-11-17T10:00:00Z')").lastrowid
        conn.execute(
            "UPDATE ledger_entries SET status='已隔离', settle_run_id=? WHERE entry_key='REV-REPAIR-001'",
            (run_id,),
        )
        conn.execute(
            "INSERT INTO quarantine_items(entry_key, account_id, reason, amount_cents, settle_run_id, created_at) "
            "VALUES ('REV-REPAIR-001', 2, '冲正无原单', 3000, ?, '2026-11-17T10:05:00Z')",
            (run_id,),
        )
        conn.commit()

    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-003", "new_key": "NEW-20261117-003"})
    assert r.status_code == 200, r.text
    body = r.json()
    # 仅未终态的 REPAIR-001 计入重指向数；已归并 003 走终态留边，不改 target
    assert body["retargeted_reversal_count"] == 1

    with connect() as conn:
        # 已终态冲正：target 留在旧键（历史配平事实不可改写）
        terminal = conn.execute(
            "SELECT target_entry_key, status FROM ledger_entries WHERE entry_key='REV-20261116-003'"
        ).fetchone()
        assert terminal["target_entry_key"] == "FWD-20261116-003"
        assert terminal["status"] == "已归并"
        # 未终态隔离冲正：target 改新键、隔离释放、复位待归并
        q = conn.execute("SELECT COUNT(*) AS c FROM quarantine_items WHERE entry_key='REV-REPAIR-001'").fetchone()["c"]
        assert q == 0
        st = conn.execute("SELECT status, target_entry_key FROM ledger_entries WHERE entry_key='REV-REPAIR-001'").fetchone()
        assert st["status"] == "待归并"
        assert st["target_entry_key"] == "NEW-20261117-003"

    # 证明实体区分两类：重指向数 1、终态留边 1，触及键全集两条
    proof = client.get(f"/api/key-retargets/{body['id']}/repoint-proof").json()
    assert proof["repointed_count"] == 1
    assert proof["terminal_edge_count"] == 1
    assert set(proof["touched_keys"]) == {"REV-20261116-003", "REV-REPAIR-001"}
    actions = {e["reversal_key"]: e["action"] for e in proof["迁移边"]}
    assert actions["REV-20261116-003"] == "终态留边"
    assert actions["REV-REPAIR-001"] == "重指向"
    terminal_edge = next(e for e in proof["迁移边"] if e["reversal_key"] == "REV-20261116-003")
    assert terminal_edge["from_target_key"] == terminal_edge["to_target_key"] == "FWD-20261116-003"

    # 冲正页同源展示终态留边证明字段
    rev_rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=冲正").json()}
    assert rev_rows["REV-20261116-003"]["重指向处置"] == "终态留边"
    assert rev_rows["REV-20261116-003"]["重指向证明id"] == proof["id"]
    assert rev_rows["REV-20261116-003"]["target_entry_key"] == "FWD-20261116-003"

    # 二次归并：终态留边冲正经家族映射找到原单；修复冲正在家族额度（36000）内配平
    run = client.post("/api/settle-runs").json()
    assert run["status"] == "完成"
    q = client.get("/api/quarantine").json()
    assert not any(x["entry_key"] == "REV-REPAIR-001" for x in q)
    rows = {e["entry_key"]: e for e in client.get("/api/entries?kind=冲正").json()}
    assert rows["REV-REPAIR-001"]["status"] == "已归并"


def test_retarget_fail_closed_rolls_back_everything_on_overage(client):
    # 新键 NEW-005 自带独立冲正 9000；旧键 005 已挂冲正 8000；家族正向额度仅 16000
    _insert_forward("NEW-20261117-005", 1, 8000)
    _insert_reversal("REV-OLD-005", 1, 8000, "FWD-20261116-005")
    _insert_reversal("REV-NEW-005", 1, 9000, "NEW-20261117-005")

    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-005", "new_key": "NEW-20261117-005"})
    assert r.status_code == 400
    fail = r.json()["detail"]
    assert fail["status"] == "闭失败"
    assert "超额" in fail["fail_reason"]
    assert fail["retargeted_reversal_count"] == 0

    # 整次迁移失败闭：冲正指向全部回到迁移前，无生效迁移边，无敞口改动，
    # 无半截 target、无迁移边、无重指向证明
    with connect() as conn:
        old_rev = conn.execute("SELECT target_entry_key FROM ledger_entries WHERE entry_key='REV-OLD-005'").fetchone()
        assert old_rev["target_entry_key"] == "FWD-20261116-005"
        new_rev = conn.execute("SELECT target_entry_key FROM ledger_entries WHERE entry_key='REV-NEW-005'").fetchone()
        assert new_rev["target_entry_key"] == "NEW-20261117-005"
        active = conn.execute("SELECT COUNT(*) AS c FROM key_retargets WHERE status='生效'").fetchone()["c"]
        # 前两个用例各一条生效迁移
        assert active == 2
        # 回滚干净：本次失败无迁移边、无证明实体（前两次生效迁移各留其边/证明）
        assert conn.execute("SELECT COUNT(*) AS c FROM retarget_touches").fetchone()["c"] == 3
        assert conn.execute("SELECT COUNT(*) AS c FROM retarget_repoint_proofs").fetchone()["c"] == 2
        closed = conn.execute("SELECT * FROM key_retargets WHERE status='闭失败' ORDER BY id DESC LIMIT 1").fetchone()
        assert closed["old_entry_key"] == "FWD-20261116-005"
        assert closed["merged_remain_cents"] == 0

    # 闭失败迁移没有证明：只读接口 404，证明实体从未落库
    pr = client.get(f"/api/key-retargets/{fail['id']}/repoint-proof")
    assert pr.status_code == 404
    # 不存在的迁移 id 同样 404
    assert client.get("/api/key-retargets/999999/repoint-proof").status_code == 404

    # 失败记录在迁移列表可查，且不影响后续归并互证
    listed = client.get("/api/key-retargets").json()
    assert any(x["status"] == "闭失败" and "超额" in x["fail_reason"] for x in listed)
    assert all(x.get("重指向证明id") is None for x in listed if x["status"] == "闭失败")
    client.post("/api/settle-runs")
    rep = client.post("/api/verify-reports").json()
    assert rep["consistent"] == 1


def test_retarget_validation_failures(client):
    # 旧键不存在
    r = client.post("/api/key-retargets", json={"old_key": "NOPE", "new_key": "FWD-20261116-001"})
    assert r.status_code == 400 and "不存在" in r.json()["detail"]["fail_reason"]
    # 新键不存在
    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-001", "new_key": "NOPE"})
    assert r.status_code == 400 and "不存在" in r.json()["detail"]["fail_reason"]
    # 跨账户
    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-001", "new_key": "FWD-20261116-004"})
    assert r.status_code == 400 and "同一账户" in r.json()["detail"]["fail_reason"]
    # 旧键已有生效迁移（用例一登记过 002）
    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-002", "new_key": "FWD-20261116-001"})
    assert r.status_code == 400 and "重复迁移" in r.json()["detail"]["fail_reason"]
    # 链式迁移：新键已是迁移目标
    r = client.post("/api/key-retargets", json={"old_key": "FWD-20261116-001", "new_key": "NEW-20261117-002"})
    assert r.status_code == 400 and "链式迁移" in r.json()["detail"]["fail_reason"]
    # 全部为闭失败，无新增生效边、无新增证明
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM key_retargets WHERE status='生效'").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) AS c FROM retarget_repoint_proofs").fetchone()["c"] == 2


def test_report_exposure_matches_single_source(client):
    # 迁移表报告的敞口必须与冲正 target、家族敞口、余额同源
    rows = client.get("/api/key-retargets").json()
    ok = [x for x in rows if x["status"] == "生效"]
    assert len(ok) == 2
    for m in ok:
        with connect() as conn:
            fwd = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE kind='正向' "
                "AND entry_key IN (?, ?)", (m["old_entry_key"], m["new_entry_key"])
            ).fetchone()["s"]
            used_now = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) s FROM ledger_entries WHERE kind='冲正' "
                "AND status != '已隔离' AND target_entry_key IN (?, ?)",
                (m["old_entry_key"], m["new_entry_key"]),
            ).fetchone()["s"]
            # 登记时刻快照恒等式：合并剩余 = 家族额度 − 登记时家族有效冲正累计
            snapshot_used = fwd - m["merged_remain_cents"]
            assert m["old_remain_cents"] >= 0 and m["new_remain_cents"] >= 0
            assert snapshot_used >= 0
        # 当前家族敞口（同源于 family_exposure / 冲正 target）
        assert m["当前家族敞口分"] == fwd - used_now
        # 生效迁移必带同源于证明实体的证明 id
        assert m["重指向证明id"] == m["id"]

    # 迁移表、敞口、余额三方同源：全量已归并净额 == 余额合计
    rep = client.post("/api/verify-reports").json()
    assert rep["consistent"] == 1
    assert rep["ledger_net_cents"] == rep["balance_sum_cents"]
