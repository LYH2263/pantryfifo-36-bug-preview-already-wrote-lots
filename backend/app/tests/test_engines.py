"""Engine-level guards: non-positive / non-finite quantities never plan
or reconcile into a deduction."""

from app.engines.fefo import consume_fefo, reconcile_ticket

LOTS = [{"id": 1, "qty_remain": 5, "expiry": "2099-01-01"}]


def test_consume_fefo_rejects_bad_qty():
    for bad in (0, -1, float("nan"), float("inf"), float("-inf")):
        r = consume_fefo(LOTS, bad)
        assert r["ok"] is False
        assert r["reason"] == "qty_non_positive"
        assert r["deductions"] == []


def test_reconcile_ticket_rejects_bad_take():
    current = {1: {"id": 1, "status": "on_shelf", "qty_remain": 5, "expiry": "2099-01-01"}}
    for bad in (0, -1, float("nan"), float("inf")):
        r = reconcile_ticket([{"lot_id": 1, "take": bad}], current, "2026-10-06")
        assert r["ok"] is False
        assert r["reason"] == "qty_non_positive"
