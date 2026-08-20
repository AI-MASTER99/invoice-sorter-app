"""Tests for the company-wide commodity-code list (the shared "V-lookup").

Covers the /api/commodity-codes endpoint validation, the derived key
columns, the tariff-shaped return of lookup_commodity_list, and — because
direct handler calls bypass FastAPI's Depends() — a route-table check that
the permission gates are the intended ones (every user can edit the list
and the clients registry; only deleting a client stays admin-gated).

Run: python -m pytest tests_commodity_codes.py -q
"""
import asyncio
import io
import sys
from pathlib import Path

import openpyxl
import pytest
from fastapi import HTTPException

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import database as db  # noqa: E402
import main as m  # noqa: E402  — relies on .env being valid

CTX = {"user_id": "u1", "username": "operator", "company_id": "c1", "role": "user"}


# ---------------------------------------------------------------------------
# POST /api/commodity-codes — validation + derived columns
# ---------------------------------------------------------------------------
def test_post_rejects_bad_code_lengths(monkeypatch):
    written = []
    monkeypatch.setattr(m.db, "upsert_commodity_code",
                        lambda cid, entry: written.append(entry) or entry)
    for bad in ["1234567", "12345678901", "", "no digits at all"]:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(m.api_upsert_commodity_code(
                body={"full_code": bad, "description": "X"}, ctx=CTX))
        assert ei.value.status_code == 400, f"code {bad!r}"
    assert written == []


def test_post_requires_description(monkeypatch):
    written = []
    monkeypatch.setattr(m.db, "upsert_commodity_code",
                        lambda cid, entry: written.append(entry) or entry)
    for body in [{"full_code": "0704901000"},
                 {"full_code": "0704901000", "description": "   "}]:
        with pytest.raises(HTTPException) as ei:
            asyncio.run(m.api_upsert_commodity_code(body=body, ctx=CTX))
        assert ei.value.status_code == 400
    assert written == []


def test_post_derives_key_columns(monkeypatch):
    """Punctuation is stripped; general_code/taric_code come from full_code."""
    saved = {}
    monkeypatch.setattr(
        m.db, "upsert_commodity_code",
        lambda cid, entry: saved.update({"company_id": cid, **entry}) or entry)
    asyncio.run(m.api_upsert_commodity_code(
        body={"full_code": "0704.90.10.00", "description": "CABBAGE"}, ctx=CTX))
    assert saved == {
        "company_id":   "c1",
        "general_code": "07049010",
        "full_code":    "0704901000",
        "taric_code":   "00",
        "description":  "CABBAGE",
    }
    # An 8-digit code is accepted with an empty TARIC part.
    saved.clear()
    asyncio.run(m.api_upsert_commodity_code(
        body={"full_code": "07049010", "description": "CABBAGE"}, ctx=CTX))
    assert saved["general_code"] == "07049010"
    assert saved["full_code"] == "07049010"
    assert saved["taric_code"] == ""


# ---------------------------------------------------------------------------
# lookup_commodity_list — the pipeline's VLOOKUP
# ---------------------------------------------------------------------------
def test_lookup_commodity_list_shape(monkeypatch):
    row = {"id": "x1", "full_code": "0704901000", "description": "CABBAGE",
           "mop": "A", "documents": []}
    monkeypatch.setattr(
        m.db, "get_commodity_codes_by_general_code",
        lambda cid, gen: [row] if (cid, gen) == ("c1", "07049010") else [])
    hit = m.lookup_commodity_list("c1", "0704.90.10")   # 8 digits after strip
    assert hit["source"] == "commodity_list"
    assert hit["description"] == "CABBAGE"
    assert hit["duty"] == "N/A" and hit["vat"] == "0%"
    [sub] = hit["subcodes"]
    assert sub["code"] == "0704901000"
    assert sub["description"] == "CABBAGE"
    assert sub["product"] is row      # the full row rides along for _cds


def test_lookup_pads_short_codes(monkeypatch):
    """7-digit invoice codes (dropped leading zero) still hit the list."""
    seen = []
    monkeypatch.setattr(m.db, "get_commodity_codes_by_general_code",
                        lambda cid, gen: seen.append(gen) or [])
    m.lookup_commodity_list("c1", "704.90.10")
    assert seen == ["07049010"]


def test_lookup_miss_is_empty_not_error(monkeypatch):
    monkeypatch.setattr(m.db, "get_commodity_codes_by_general_code",
                        lambda cid, gen: [])
    miss = m.lookup_commodity_list("c1", "99999999")
    assert miss["subcodes"] == [] and miss["description"] == ""


# ---------------------------------------------------------------------------
# Route gates — Depends() is bypassed by direct handler calls, so assert the
# wiring on the route table itself.
# ---------------------------------------------------------------------------
def _route(path: str, method: str):
    for r in m.app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set()):
            return r
    raise AssertionError(f"route {method} {path} not found")


def _gates(route) -> set:
    return {d.call for d in route.dependant.dependencies}


def test_every_user_can_edit_lists_and_registry():
    for path, method in [
        ("/api/commodity-codes", "GET"),
        ("/api/commodity-codes", "POST"),
        ("/api/commodity-codes/import", "POST"),
        ("/api/commodity-codes/{code_id}", "DELETE"),
        ("/api/clients", "GET"),
        ("/api/clients", "POST"),
        ("/api/clients/{client_id}", "PUT"),
    ]:
        gates = _gates(_route(path, method))
        assert m.authed in gates, f"{method} {path} lost its auth gate"
        assert m.admin_authed not in gates, f"{method} {path} is admin-gated"


def test_client_delete_stays_admin_only():
    gates = _gates(_route("/api/clients/{client_id}", "DELETE"))
    assert m.admin_authed in gates


def test_per_client_product_routes_are_gone():
    paths = {getattr(r, "path", "") for r in m.app.routes}
    leftovers = {p for p in paths if p.startswith("/api/clients/{client_id}/products")}
    assert not leftovers, f"per-client product routes still registered: {leftovers}"


# ---------------------------------------------------------------------------
# POST /api/commodity-codes/import — bulk-add a sheet, skipping what's there
# ---------------------------------------------------------------------------
class _Upload:
    """The slice of UploadFile the handler uses (chunked async read)."""

    def __init__(self, data: bytes, filename="codes.csv"):
        self.filename = filename
        self._buf = io.BytesIO(data)

    async def read(self, n=-1):
        return self._buf.read(n)


SHEET = (b"Commodity Code,Additional Taric,Description\r\n"
         b"07049010,00,CABBAGE\r\n"           # already in the list
         b"02013000,90,TARTARE DI FASONA\r\n"  # new
         b"852910,00,ELEVATOR MATERIAL\r\n")   # 6-digit stub, unusable


def _stub_list(monkeypatch, existing=(("07049010", "0704901000"),)):
    written = []
    monkeypatch.setattr(m.db, "list_commodity_codes",
                        lambda cid: [{"general_code": g, "full_code": f}
                                     for g, f in existing])
    monkeypatch.setattr(m.db, "upsert_commodity_codes",
                        lambda cid, rows: written.extend(rows) or len(rows))
    monkeypatch.setattr(m.db, "count_commodity_codes",
                        lambda cid: len(existing) + len(written))
    return written


def test_import_adds_only_the_codes_not_already_there(monkeypatch):
    written = _stub_list(monkeypatch)
    out = asyncio.run(m.api_import_commodity_codes(file=_Upload(SHEET), ctx=CTX))
    assert out == {"added": 1, "already_present": 1, "skipped": 1, "total": 2}
    # Only the new code is written, in the commodity_codes column shape.
    assert written == [{"general_code": "02013000", "full_code": "0201300090",
                        "taric_code": "90", "description": "TARTARE DI FASONA"}]


def test_import_of_an_entirely_known_sheet_writes_nothing(monkeypatch):
    written = _stub_list(monkeypatch, existing=(("07049010", "0704901000"),
                                                ("02013000", "0201300090")))
    out = asyncio.run(m.api_import_commodity_codes(file=_Upload(SHEET), ctx=CTX))
    assert (out["added"], out["already_present"]) == (0, 2)
    assert written == []


def test_import_reads_an_xlsx_by_its_magic_not_its_name(monkeypatch):
    written = _stub_list(monkeypatch, existing=())
    wb = openpyxl.Workbook()
    wb.active.append(["Commodity Code", "Additional Taric", "Description"])
    wb.active.append(["07049010", "00", "CABBAGE"])
    buf = io.BytesIO()
    wb.save(buf)
    out = asyncio.run(m.api_import_commodity_codes(
        file=_Upload(buf.getvalue(), "list.xlsx"), ctx=CTX))
    assert out["added"] == 1
    assert written[0]["description"] == "CABBAGE"


def test_import_rejects_a_wrong_file_type():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(m.api_import_commodity_codes(
            file=_Upload(b"%PDF-1.4", "codes.pdf"), ctx=CTX))
    assert ei.value.status_code == 400


def test_import_rejects_a_sheet_without_the_columns():
    sheet = b"Widget,Colour\r\nspanner,red\r\n"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(m.api_import_commodity_codes(file=_Upload(sheet), ctx=CTX))
    assert ei.value.status_code == 400
    assert "description" in ei.value.detail


def test_import_rejects_a_sheet_with_no_usable_code():
    sheet = b"Commodity Code,Description\r\n852910,ELEVATOR MATERIAL\r\n"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(m.api_import_commodity_codes(file=_Upload(sheet), ctx=CTX))
    assert ei.value.status_code == 400


def test_import_refuses_an_oversized_file():
    big = b"Commodity Code,Description\r\n" + b"x" * (m.MAX_CODE_LIST_BYTES + 1)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(m.api_import_commodity_codes(file=_Upload(big), ctx=CTX))
    assert ei.value.status_code == 413


# ---------------------------------------------------------------------------
# db.list_commodity_codes — paged past PostgREST's row cap
# ---------------------------------------------------------------------------
class _FakeQuery:
    """Records the chained call and serves one page out of `rows`."""

    def __init__(self, rows, calls):
        self._rows, self._calls = rows, calls
        self._order = []

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

    def order(self, col):
        self._order.append(col)
        return self

    def range(self, start, end):
        self._span = (start, end)
        return self

    def execute(self):
        start, end = self._span
        self._calls.append((start, end, tuple(self._order)))
        return type("R", (), {"data": self._rows[start:end + 1]})()


class _FakeClient:
    def __init__(self, rows, calls):
        self._rows, self._calls = rows, calls

    def table(self, name):
        return _FakeQuery(self._rows, self._calls)


def _fake_db(monkeypatch, n_rows):
    rows = [{"full_code": f"{i:010d}"} for i in range(n_rows)]
    calls = []
    monkeypatch.setattr(db, "_client", lambda: _FakeClient(rows, calls))
    return rows, calls


def test_list_commodity_codes_returns_more_than_one_page(monkeypatch):
    """A 1714-code list must come back whole, not capped at 1000."""
    rows, calls = _fake_db(monkeypatch, 1714)
    got = db.list_commodity_codes("c1")
    assert len(got) == 1714 and got == rows
    assert [(s, e) for s, e, _ in calls] == [(0, 999), (1000, 1999)]


def test_list_commodity_codes_stops_on_a_short_page(monkeypatch):
    _, calls = _fake_db(monkeypatch, 42)
    assert len(db.list_commodity_codes("c1")) == 42
    assert len(calls) == 1                      # no needless second request


def test_list_commodity_codes_pages_on_a_total_order(monkeypatch):
    """A non-unique sort key could skip or repeat rows across pages."""
    _, calls = _fake_db(monkeypatch, 1200)
    db.list_commodity_codes("c1")
    assert calls[0][2] == ("general_code", "full_code")


def test_exact_multiple_of_the_page_size_needs_the_empty_page(monkeypatch):
    _, calls = _fake_db(monkeypatch, 2000)
    assert len(db.list_commodity_codes("c1")) == 2000
    assert len(calls) == 3                      # 1000 + 1000 + the short one
