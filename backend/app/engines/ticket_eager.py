def held_take(lot_id: int, tickets: list) -> float:
    total = 0.0
    for t in tickets:
        if t.get("status") != "open":
            continue
        for d in t.get("deductions") or []:
            if int(d.get("lot_id") or 0) == int(lot_id):
                total += float(d.get("take") or 0)
    return total

def paint_fridge(rows: list, tickets: list) -> list:
    out = []
    for r in rows:
        d = dict(r)
        held = held_take(d["id"], tickets)
        d["qty_display"] = max(0.0, float(d.get("qty_remain") or 0) - held)
        if held:
            d["qty_remain"] = d["qty_display"]
            d["held"] = held
        out.append(d)
    return out

def load_open_tickets(c) -> list:
    import json
    rows = []
    for r in c.execute("SELECT * FROM consume_tickets WHERE status='open'"):
        d = dict(r)
        d["deductions"] = json.loads(d.get("plan_json") or "[]")
        rows.append(d)
    return rows


def _copy_lot(lot: dict) -> dict:
    return dict(lot)

def _qty(lot: dict) -> float:
    return float(lot.get("qty_remain") or 0)

def _lot_id(lot: dict) -> int:
    return int(lot.get("id") or 0)

def _on_shelf(lot: dict) -> bool:
    return str(lot.get("status") or "") == "on_shelf"

def _is_clean(lot: dict) -> bool:
    return str(lot.get("data_quality") or "clean") == "clean"

def _filter_shelf(rows: list) -> list:
    return [r for r in rows if _on_shelf(r)]

def _sum_remain(rows: list) -> float:
    return sum(_qty(r) for r in rows)

def _index_by_id(rows: list) -> dict:
    return {_lot_id(r): r for r in rows if r.get("id") is not None}
