from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

TEST_DIR = ROOT / "backend" / "data_test"


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

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["项目"] == "冲流台"


def test_settle_and_verify(client):
    d = client.get("/api/dashboard").json()
    assert d["账户数"] >= 3
    assert d["正向笔数"] >= 5
    assert d["冲正笔数"] >= 4

    run = client.post("/api/settle-runs")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "完成"
    assert body["quarantine_count"] >= 1

    q = client.get("/api/quarantine").json()
    assert any(x["reason"] == "冲正无原单" for x in q)

    report = client.post("/api/verify-reports")
    assert report.status_code == 200, report.text
    rj = report.json()
    assert rj["consistent"] == 1
    assert rj["ledger_net_cents"] == rj["balance_sum_cents"]
