"""Tests for the company-wide commodity-code list (the shared "V-lookup").

Covers the /api/commodity-codes endpoint validation, the derived key
columns, the tariff-shaped return of lookup_commodity_list, and — because
direct handler calls bypass FastAPI's Depends() — a route-table check that
the permission gates are the intended ones (every user can edit the list
and the clients registry; only deleting a client stays admin-gated).

Run: python -m pytest tests_commodity_codes.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

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
