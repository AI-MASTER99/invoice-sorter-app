"""Standalone smoke tests for user-management endpoints — privilege gate.

Runs in-process against `main.py` with stubbed `db.get_user` /
`db.create_user` so the tests don't touch Supabase. Invoke with:

    cd invoiceflow && python tests_user_admin.py

Exits 0 on pass, non-zero on any failure. Covers the role whitelist
+ super_admin gate added to `api_add_user` after a security review
caught a privilege-escalation path: a regular `admin` could POST
{"role": "super_admin"} and self-elevate to cross-tenant access.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import main as m  # noqa: E402  — relies on .env being valid
from fastapi import HTTPException  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs — keep tests off the network/DB.
# ---------------------------------------------------------------------------
_created: list[dict] = []


def _stub_db() -> None:
    m.db.get_user = lambda username, company_id: None  # always "doesn't exist"
    m.db.create_user = lambda company_id, username, password_hash, role: (
        _created.append({
            "company_id": company_id,
            "username": username,
            "role": role,
        }) or {"id": "fake", "username": username, "role": role}
    )


def _step(msg: str) -> None:
    print(f"  → {msg}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_admin_cannot_create_super_admin() -> None:
    print("[1] admin POST role=super_admin → 403")
    _created.clear()
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    try:
        asyncio.run(m.api_add_user(
            body={"username": "evil", "password": "pwd123", "role": "super_admin"},
            ctx=admin_ctx,
        ))
    except HTTPException as e:
        assert e.status_code == 403, f"expected 403, got {e.status_code}"
        assert _created == [], "user should not have been created"
        _step("regular admin blocked from minting super_admin")
        return
    raise AssertionError("admin was allowed to create super_admin — privilege escalation!")


def test_admin_can_create_normal_user() -> None:
    print("[2] admin POST role=user → success")
    _created.clear()
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    asyncio.run(m.api_add_user(
        body={"username": "newuser", "password": "pwd123", "role": "user"},
        ctx=admin_ctx,
    ))
    assert len(_created) == 1
    assert _created[0]["role"] == "user"
    assert _created[0]["company_id"] == "c1"
    _step("admin can create normal user in own company")


def test_admin_can_create_another_admin() -> None:
    print("[3] admin POST role=admin → success")
    _created.clear()
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    asyncio.run(m.api_add_user(
        body={"username": "newadmin", "password": "pwd123", "role": "admin"},
        ctx=admin_ctx,
    ))
    assert _created[0]["role"] == "admin"
    _step("admin can create another admin in own company")


def test_invalid_role_rejected() -> None:
    print("[4] invalid role string → 400")
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    for bad in ["root", "owner", "anonymous", "../admin", "admin\x00", "ADMIN_X"]:
        _created.clear()
        try:
            asyncio.run(m.api_add_user(
                body={"username": "x", "password": "y", "role": bad},
                ctx=admin_ctx,
            ))
        except HTTPException as e:
            assert e.status_code == 400, f"role {bad!r}: expected 400, got {e.status_code}"
            assert _created == []
            continue
        raise AssertionError(f"invalid role {bad!r} was accepted")
    _step("rejects unknown roles, null bytes, traversal-like inputs")


def test_super_admin_can_create_super_admin() -> None:
    print("[5] super_admin POST role=super_admin → success")
    _created.clear()
    super_ctx = {"user_id": "s1", "username": "super", "company_id": "c1", "role": "super_admin"}
    asyncio.run(m.api_add_user(
        body={"username": "super2", "password": "pwd", "role": "super_admin"},
        ctx=super_ctx,
    ))
    assert _created[0]["role"] == "super_admin"
    _step("super_admin can mint another super_admin")


def test_empty_username_rejected() -> None:
    print("[6] empty username/password → 400")
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    for body in [
        {"username": "", "password": "x", "role": "user"},
        {"username": "ok", "password": "", "role": "user"},
        {},
    ]:
        try:
            asyncio.run(m.api_add_user(body=body, ctx=admin_ctx))
        except HTTPException as e:
            assert e.status_code == 400
            continue
        raise AssertionError(f"empty body {body!r} was accepted")
    _step("missing username or password rejected")


def test_role_case_normalization() -> None:
    print("[7] role normalized to lowercase")
    _created.clear()
    admin_ctx = {"user_id": "a1", "username": "admin1", "company_id": "c1", "role": "admin"}
    asyncio.run(m.api_add_user(
        body={"username": "u1", "password": "x", "role": "ADMIN"},
        ctx=admin_ctx,
    ))
    assert _created[0]["role"] == "admin"
    _step("uppercase ADMIN normalized to admin (whitelist match)")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
TESTS = [
    test_admin_cannot_create_super_admin,
    test_admin_can_create_normal_user,
    test_admin_can_create_another_admin,
    test_invalid_role_rejected,
    test_super_admin_can_create_super_admin,
    test_empty_username_rejected,
    test_role_case_normalization,
]


def main() -> int:
    _stub_db()
    failures: list[str] = []
    for fn in TESTS:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  FAIL: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{fn.__name__}: unexpected {type(e).__name__}: {e}")
            print(f"  ERROR: {type(e).__name__}: {e}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}/{len(TESTS)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED ({len(TESTS)}/{len(TESTS)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
