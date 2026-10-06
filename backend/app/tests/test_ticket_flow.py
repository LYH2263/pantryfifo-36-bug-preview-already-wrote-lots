"""End-to-end invariants for the preview -> confirm consume flow.

Core contract under test:
- preview only issues a ticket naming lots/takes; shelf totals, layer views
  and the alert bar keep showing the real qty_remain;
- a confirmed ticket deducts exactly the pinned lots/quantities, and the
  read-back shelf matches the preview numbers;
- if a pinned lot was swept / consumed / shrunk before confirm, the whole
  ticket fails atomically: status 'failed', zero qty_remain change;
- non-positive or non-finite qty is a 400 and leaves no ticket behind.
"""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import seed  # noqa: F401
    from app.main import app
    from app import db
    with TestClient(app) as c:
        yield c, db


def _add_item(c, db, name="测试物料"):
    conn = db.connect()
    cur = conn.execute("INSERT INTO items(name,layer,unit) VALUES (?,?,?)",
                       (name, "mid", "份"))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


def _add_lot(c, item_id, qty, expiry):
    r = c.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": expiry})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _fridge(c, item_id=None):
    rows = c.get("/api/fridge").json()
    if item_id is not None:
        rows = [r for r in rows if r["item_id"] == item_id]
    return rows


def _total(c, item_id=None):
    return sum(float(r["qty_remain"]) for r in _fridge(c, item_id))


def _lot_map(c, item_id):
    return {r["id"]: r for r in _fridge(c, item_id)}


def _ticket_status(db, token):
    conn = db.connect()
    s = conn.execute("SELECT status FROM consume_tickets WHERE token=?",
                     (token,)).fetchone()["status"]
    conn.close()
    return s


def test_preview_does_not_move_shelf_or_alerts(client):
    c, db = client
    item = _add_item(c, db)
    soon_exp = (date.today() + timedelta(days=1)).isoformat()
    soon_id = _add_lot(c, item, 4, soon_exp)
    _add_lot(c, item, 10, "2030-01-01")

    total_before = _total(c)
    alerts_before = {a["id"]: a for a in c.get("/api/alerts").json()}

    r = c.post("/api/consume/preview", json={"item_id": item, "qty": 3})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert [d["lot_id"] for d in pv["deductions"]] == [soon_id]
    assert pv["deductions"][0]["take"] == 3

    # Shelf still shows the real remaining quantity, no virtual-hold fields.
    after = _lot_map(c, item)
    assert _total(c) == total_before
    assert float(after[soon_id]["qty_remain"]) == 4.0
    assert "held" not in after[soon_id] and "qty_display" not in after[soon_id]

    # Alert bar is untouched too: the soon lot still reports its full qty.
    alerts_after = {a["id"]: a for a in c.get("/api/alerts").json()}
    assert float(alerts_after[soon_id]["qty_remain"]) == 4.0
    assert alerts_after[soon_id]["level"] == "soon"
    assert set(alerts_before) == set(alerts_after)


def test_confirm_deducts_exactly_preview_plan_and_reads_back(client):
    c, db = client
    item = _add_item(c, db)
    _add_lot(c, item, 4, (date.today() + timedelta(days=1)).isoformat())
    _add_lot(c, item, 10, "2030-01-01")

    pv = c.post("/api/consume/preview", json={"item_id": item, "qty": 3}).json()
    before = _lot_map(c, item)
    total_before = _total(c)

    r = c.post("/api/consume/confirm", json={"token": pv["token"]})
    assert r.status_code == 200, r.text
    ok = r.json()
    # Confirmation applies the same lot/take list the preview promised.
    assert ok["deductions"] == pv["deductions"]

    after = _lot_map(c, item)
    for d in pv["deductions"]:
        lid = d["lot_id"]
        assert float(after[lid]["qty_remain"]) == pytest.approx(
            float(before[lid]["qty_remain"]) - d["take"])
    assert _total(c) == pytest.approx(total_before - 3)
    assert _ticket_status(db, pv["token"]) == "confirmed"


def test_sweep_between_preview_and_confirm_fails_whole_ticket_without_deducting(client):
    c, db = client
    item = _add_item(c, db)
    lid = _add_lot(c, item, 4, (date.today() + timedelta(days=2)).isoformat())

    pv = c.post("/api/consume/preview", json={"item_id": item, "qty": 3}).json()
    assert [d["lot_id"] for d in pv["deductions"]] == [lid]

    # Rollover + expiry sweep removes the pinned lot from the shelf.
    conn = db.connect()
    conn.execute("UPDATE lots SET expiry=? WHERE id=?",
                 ((date.today() - timedelta(days=1)).isoformat(), lid))
    conn.commit(); conn.close()
    swept = c.post("/api/expire-sweep", json={}).json()["expired_ids"]
    assert lid in swept

    shelf_before_confirm = c.get("/api/fridge").json()

    r = c.post("/api/consume/confirm", json={"token": pv["token"]})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["ok"] is False and body["reason"] == "lot_expired"
    assert body["short"] == 3 and body["deductions"] == pv["deductions"]

    # Nothing about the shelf changes because of the failed confirm...
    assert c.get("/api/fridge").json() == shelf_before_confirm
    # ...and the swept lot kept every unit it had when the ticket was previewed.
    conn = db.connect()
    row = conn.execute("SELECT status,qty_remain FROM lots WHERE id=?",
                       (lid,)).fetchone()
    conn.close()
    assert row["status"] == "expired" and float(row["qty_remain"]) == 4.0
    assert _ticket_status(db, pv["token"]) == "failed"


def test_two_overlapping_tickets_only_one_lands_and_total_matches_that_one(client):
    c, db = client
    item = _add_item(c, db)
    near = _add_lot(c, item, 12, "2030-02-01")
    _add_lot(c, item, 10, "2031-01-01")
    total_before = _total(c, item)

    t1 = c.post("/api/consume/preview", json={"item_id": item, "qty": 8}).json()
    t2 = c.post("/api/consume/preview", json={"item_id": item, "qty": 8}).json()
    # Both previews independently pin 8 from the same earliest lot.
    assert t1["deductions"] == [{"lot_id": near, "take": 8, "expiry": "2030-02-01"}]
    assert t2["deductions"] == t1["deductions"]

    r1 = c.post("/api/consume/confirm", json={"token": t1["token"]})
    assert r1.status_code == 200, r1.text
    assert float(_lot_map(c, item)[near]["qty_remain"]) == 4.0

    r2 = c.post("/api/consume/confirm", json={"token": t2["token"]})
    assert r2.status_code == 409
    body = r2.json()["detail"]
    assert body["reason"] == "short" and body["short"] == 4

    # The shelf lost exactly the one successful ticket's 8 units — never a
    # quantity that matches neither response.
    assert _total(c, item) == pytest.approx(total_before - 8)
    assert _ticket_status(db, t1["token"]) == "confirmed"
    assert _ticket_status(db, t2["token"]) == "failed"


@pytest.mark.parametrize("qty", [0, -1, -0.01])
def test_non_positive_qty_fails_and_leaves_no_ticket(client, qty):
    c, db = client
    item = _add_item(c, db)
    _add_lot(c, item, 5, "2030-01-01")
    total_before = _total(c)

    r = c.post("/api/consume/preview", json={"item_id": item, "qty": qty})
    assert r.status_code == 400
    assert _total(c) == total_before

    r = c.post("/api/consume", json={"item_id": item, "qty": qty})
    assert r.status_code == 400
    assert _total(c) == total_before

    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) n FROM consume_tickets").fetchone()["n"]
    conn.close()
    assert n == 0


def test_nan_qty_fails_without_ticket(client):
    c, db = client
    item = _add_item(c, db)
    _add_lot(c, item, 5, "2030-01-01")
    r = c.post("/api/consume/preview",
               content=json.dumps({"item_id": item, "qty": float("nan")}),
               headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) n FROM consume_tickets").fetchone()["n"]
    conn.close()
    assert n == 0


def test_short_preview_and_double_confirm_leave_stock_intact(client):
    c, db = client
    item = _add_item(c, db)
    _add_lot(c, item, 2, "2030-01-01")
    total_before = _total(c)

    r = c.post("/api/consume/preview", json={"item_id": item, "qty": 5})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"
    assert _total(c) == total_before
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) n FROM consume_tickets").fetchone()["n"] == 0
    conn.close()

    pv = c.post("/api/consume/preview", json={"item_id": item, "qty": 2}).json()
    assert c.post("/api/consume/confirm", json={"token": pv["token"]}).status_code == 200
    total_after_first = _total(c)
    r2 = c.post("/api/consume/confirm", json={"token": pv["token"]})
    assert r2.status_code == 409 and r2.json()["detail"]["reason"] == "ticket_closed"
    assert _total(c) == total_after_first
