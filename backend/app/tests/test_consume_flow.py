"""Alignment contract for the preview -> confirm consume flow.

One semantics everywhere (master table / layer pages / urgent bar):
  * a preview only writes a ticket — no read view moves, no qty_remain moves;
  * confirm is all-or-nothing against the commit-instant state, never a
    recalculation: success applies exactly the previewed deductions, failure
    burns the ticket and leaves every qty_remain untouched;
  * a non-positive (or non-finite) quantity fails outright and leaves no
    ticket row behind.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

FAR_FUTURE = "2099-01-01"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield SimpleNamespace(client=c, db=tmp_path / "pantryfifo.db")


def db(env, sql, args=()):
    c = sqlite3.connect(env.db)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(sql, args)]
    c.commit()
    c.close()
    return rows


def remains(env):
    """lot_id -> qty_remain for every lot, straight from the db."""
    return {r["id"]: r["qty_remain"] for r in db(env, "SELECT id, qty_remain FROM lots")}


def ticket_count(env):
    return db(env, "SELECT COUNT(*) c FROM consume_tickets")[0]["c"]


def inbound(env, item_id=1, qty=12, expiry=FAR_FUTURE):
    r = env.client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})
    assert r.status_code == 200
    return r.json()["id"]


def preview(env, item_id=1, qty=5):
    return env.client.post("/api/consume/preview", json={"item_id": item_id, "qty": qty})


def confirm(env, token):
    return env.client.post("/api/consume/confirm", json={"token": token})


def test_preview_leaves_fridge_and_alerts_untouched(env):
    lot_id = inbound(env, qty=12)
    fridge_before = env.client.get("/api/fridge").json()
    alerts_before = env.client.get("/api/alerts").json()

    r = preview(env, qty=5)
    assert r.status_code == 200
    body = r.json()
    assert body["deductions"] == [{"lot_id": lot_id, "take": 5, "expiry": FAR_FUTURE}]
    assert body["short"] == 0.0

    # neither the master table nor the urgent bar may move on a preview
    assert env.client.get("/api/fridge").json() == fridge_before
    assert env.client.get("/api/alerts").json() == alerts_before
    # and no view may carry a painted "occupied" quantity
    for row in env.client.get("/api/fridge").json() + env.client.get("/api/alerts").json():
        assert "held" not in row and "qty_display" not in row
    assert remains(env)[lot_id] == 12


def test_confirm_success_readback_matches_preview(env):
    lot_id = inbound(env, qty=12)
    before = remains(env)
    plan = preview(env, qty=5).json()

    r = confirm(env, plan["token"])
    assert r.status_code == 200
    # the confirm response applies exactly what the preview promised
    assert r.json()["deductions"] == plan["deductions"]
    assert r.json()["short"] == 0.0

    # read-back: exactly the previewed takes landed, nothing else moved
    after = remains(env)
    assert after[lot_id] == before[lot_id] - 5
    for lid, qty in before.items():
        if lid != lot_id:
            assert after[lid] == qty
    row = [x for x in env.client.get("/api/fridge").json() if x["id"] == lot_id][0]
    assert row["qty_remain"] == 7


def test_sweep_then_confirm_fails_whole_and_moves_nothing(env):
    lot_id = inbound(env, qty=12)
    before = remains(env)
    token = preview(env, qty=5).json()["token"]

    # day rolls over between preview and confirm: the sweep delists the lot
    db(env, "UPDATE lots SET expiry='2000-01-01' WHERE id=?", (lot_id,))
    swept = env.client.post("/api/expire-sweep")
    assert lot_id in swept.json()["expired_ids"]
    fridge_after_sweep = env.client.get("/api/fridge").json()

    r = confirm(env, token)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["ok"] is False
    assert detail["reason"] in ("lot_expired", "lot_changed")
    assert detail["short"] == 5

    # the failed confirm moved nothing: table == post-sweep state, and every
    # qty_remain is still exactly what it was before the preview
    assert env.client.get("/api/fridge").json() == fridge_after_sweep
    assert remains(env) == before

    # the ticket is burned: a stacked confirm can never land a second outcome
    r2 = confirm(env, token)
    assert r2.status_code == 409
    assert r2.json()["detail"]["reason"] == "ticket_closed"
    assert remains(env) == before


def test_two_tickets_one_success_one_failure_table_matches_success(env):
    lot_id = inbound(env, qty=12)
    a = preview(env, qty=5).json()
    b = preview(env, qty=8).json()

    ra = confirm(env, a["token"])
    assert ra.status_code == 200
    fridge_after_a = env.client.get("/api/fridge").json()

    rb = confirm(env, b["token"])
    assert rb.status_code == 409
    detail = rb.json()["detail"]
    assert detail["reason"] == "short"
    assert detail["short"] == 1  # 12 - 5 landed by A, B needed 8

    # the shelf dropped by exactly A's response (5), B contributed nothing
    assert remains(env)[lot_id] == 12 - 5
    assert env.client.get("/api/fridge").json() == fridge_after_a


def test_legacy_consume_racing_ticket_empties_lot_then_confirm_fails(env):
    lot_id = inbound(env, qty=12)
    token = preview(env, qty=5).json()["token"]

    r = env.client.post("/api/consume", json={"item_id": 1, "qty": 12})
    assert r.status_code == 200
    fridge_after_consume = env.client.get("/api/fridge").json()

    rc = confirm(env, token)
    assert rc.status_code == 409
    assert rc.json()["detail"]["reason"] == "lot_changed"
    # only the legacy consume's own response moved the shelf
    assert remains(env)[lot_id] == 0
    assert env.client.get("/api/fridge").json() == fridge_after_consume


def test_non_positive_and_non_finite_qty_leave_no_ticket(env):
    fridge_before = env.client.get("/api/fridge").json()
    tickets_before = ticket_count(env)

    for qty in (0, -2):
        r = preview(env, qty=qty)
        assert r.status_code == 400
        assert r.json()["detail"] == "qty_non_positive"
        r = env.client.post("/api/consume", json={"item_id": 1, "qty": qty})
        assert r.status_code == 400

    for literal in ("NaN", "Infinity", "-Infinity"):
        r = env.client.post(
            "/api/consume/preview",
            content=json.dumps({"item_id": 1, "qty": json.loads(literal)}),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "qty_non_positive"

    # no fake ticket rows, no shelf movement
    assert ticket_count(env) == tickets_before
    assert env.client.get("/api/fridge").json() == fridge_before


def test_shortage_preview_fails_and_leaves_no_ticket(env):
    inbound(env, qty=12)
    tickets_before = ticket_count(env)
    r = preview(env, qty=999)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "short"
    assert r.json()["detail"]["short"] == 987
    assert ticket_count(env) == tickets_before
