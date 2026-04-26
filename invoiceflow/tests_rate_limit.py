"""Standalone smoke tests for the login rate limiter.

Runs in-process against `main.py` — no pytest needed, no DB calls.
Invoke with:

    cd invoiceflow && python tests_rate_limit.py

Exits 0 on pass, non-zero on any assertion failure. Designed for the
Batch 1 (auth + secrets hardening) verification round so future
changes to the rate-limiter code can be re-verified with one command.

These are smoke tests, not exhaustive unit tests — they cover the
specific scenarios that prior security-review rounds caught:
  • NAT collateral lockout (round 2)
  • Eviction-orphan timestamps  (round 3)
  • 429 logging without username (round 3)
"""
from __future__ import annotations

import io
import logging
import sys
import time
from pathlib import Path

# Allow `python tests_rate_limit.py` from invoiceflow/ or repo root.
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import main as m  # noqa: E402  — relies on .env being valid
from fastapi import HTTPException  # noqa: E402


def _reset() -> None:
    m._LOGIN_ATTEMPTS_USER.clear()
    m._LOGIN_ATTEMPTS_IP.clear()


def _step(name: str) -> None:
    print(f"  → {name}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_nat_collateral_lockout_fix() -> None:
    """Round-2 fix: alice succeeding shouldn't free bob's contributions."""
    print("[1] NAT collateral lockout fix")
    _reset()
    ip = "203.0.113.42"
    for _ in range(3):
        m._record_login_failure("alice", ip)
        m._record_login_failure("bob",   ip)
    assert len(m._LOGIN_ATTEMPTS_IP[ip]) == 6
    _step("6 IP-level + 3-each user-level recorded")

    m._clear_login_failures("alice", ip)
    assert ("alice", ip) not in m._LOGIN_ATTEMPTS_USER
    assert len(m._LOGIN_ATTEMPTS_USER[("bob", ip)]) == 3
    assert len(m._LOGIN_ATTEMPTS_IP[ip]) == 3, m._LOGIN_ATTEMPTS_IP[ip]
    _step("alice success removed only her 3 timestamps; bob untouched")

    m._clear_login_failures("bob", ip)
    assert ip not in m._LOGIN_ATTEMPTS_IP
    _step("bob success cleaned up empty IP key (no zombie entries)")


def test_attacker_residue_remains() -> None:
    """A legitimate success doesn't relieve attacker pressure on the IP cap."""
    print("[2] Attacker residue remains after legitimate success")
    _reset()
    ip = "198.51.100.7"
    for i in range(49):
        m._record_login_failure(f"attacker{i}", ip)
    m._record_login_failure("charlie", ip)
    m._clear_login_failures("charlie", ip)
    assert len(m._LOGIN_ATTEMPTS_IP[ip]) == 49
    _step("49 attacker entries remain after charlie's success")


def test_eviction_cascade_no_orphans() -> None:
    """Round-3 fix: user-dict eviction cascades into the IP bucket so the
    success path doesn't leak orphans."""
    print("[3] Eviction cascade — no orphan timestamps")
    orig_cap = m._MAX_TRACKED_KEYS
    try:
        m._MAX_TRACKED_KEYS = 5
        _reset()

        ip_alice = "192.0.2.10"
        for _ in range(3):
            m._record_login_failure("alice", ip_alice)
            time.sleep(0.001)

        ip_attacker = "198.51.100.99"
        for i in range(4):
            time.sleep(0.001)
            m._record_login_failure(f"fakeuser{i}", ip_attacker)

        # 5 entries fit; no eviction yet
        assert len(m._LOGIN_ATTEMPTS_USER) == 5
        assert ("alice", ip_alice) in m._LOGIN_ATTEMPTS_USER
        _step("5 entries fit without eviction")

        # 6th entry triggers eviction; alice (oldest last_ts) gets dropped
        time.sleep(0.001)
        m._record_login_failure("fakeuser4", ip_attacker)

        assert ("alice", ip_alice) not in m._LOGIN_ATTEMPTS_USER
        assert ip_alice not in m._LOGIN_ATTEMPTS_IP, (
            f"orphan timestamps remain: {m._LOGIN_ATTEMPTS_IP.get(ip_alice)}"
        )
        _step("alice evicted AND her IP-bucket timestamps cascade-removed")

        # Attacker IP still tracked with all 5 of their attempts
        assert len(m._LOGIN_ATTEMPTS_IP[ip_attacker]) == 5
        _step("attacker IP bucket retained (no false cascade)")
    finally:
        m._MAX_TRACKED_KEYS = orig_cap


def test_nat_eviction_partial_recovery() -> None:
    """NAT scenario: bob evicted under load, colleagues can still succeed-clean."""
    print("[4] NAT eviction — partial recovery still works")
    orig_cap = m._MAX_TRACKED_KEYS
    try:
        m._MAX_TRACKED_KEYS = 5
        _reset()

        shared_ip = "203.0.113.50"
        for _ in range(2):
            m._record_login_failure("bob", shared_ip)
            time.sleep(0.001)
        for i in range(5):
            time.sleep(0.001)
            m._record_login_failure(f"colleague{i}", shared_ip)

        assert ("bob", shared_ip) not in m._LOGIN_ATTEMPTS_USER
        assert len(m._LOGIN_ATTEMPTS_IP[shared_ip]) == 5
        _step("bob evicted, his 2 timestamps cascaded out of IP bucket")

        m._clear_login_failures("colleague0", shared_ip)
        assert len(m._LOGIN_ATTEMPTS_IP[shared_ip]) == 4
        _step("colleague0 success path still works after eviction")
    finally:
        m._MAX_TRACKED_KEYS = orig_cap


def test_429_logging_no_username_leak() -> None:
    """Round-3 fix: 429 logs include IP + count but never the username."""
    print("[5] 429 logging without username leak")
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    m.logger.addHandler(handler)
    m.logger.setLevel(logging.WARNING)
    try:
        _reset()
        ip = "198.51.100.123"
        for _ in range(5):
            m._record_login_failure("victim", ip)
        try:
            m._check_login_rate_limit("victim", ip)
            raise AssertionError("expected 429")
        except HTTPException as e:
            assert e.status_code == 429
        logs = log_stream.getvalue()
        assert "victim" not in logs, f"username leaked into log: {logs!r}"
        assert ip in logs
        assert "per-user+IP" in logs
        _step("per-user+IP 429 logged with IP, no username")

        log_stream.truncate(0); log_stream.seek(0)
        _reset()
        broad_ip = "198.51.100.200"
        for i in range(50):
            m._record_login_failure(f"u{i}", broad_ip)
        try:
            m._check_login_rate_limit("newvictim", broad_ip)
            raise AssertionError("expected 429")
        except HTTPException as e:
            assert e.status_code == 429
        logs = log_stream.getvalue()
        assert "per-IP" in logs
        assert broad_ip in logs
        _step("per-IP 429 logged correctly")
    finally:
        m.logger.removeHandler(handler)


def test_x_forwarded_for_parsing() -> None:
    print("[6] X-Forwarded-For parsing")

    class _C:
        host = "10.0.0.1"

    class _R:
        def __init__(self, headers, has_client=False):
            self.headers = headers
            self.client = _C() if has_client else None

    assert m._client_ip(_R({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})) == "203.0.113.5"
    assert m._client_ip(_R({}, has_client=True)) == "10.0.0.1"
    assert m._client_ip(_R({})) == "unknown"
    _step("XFF leftmost, fallback chain, and unknown all correct")


def test_no_op_clear_path() -> None:
    print("[7] _clear_login_failures is no-op when nothing tracked")
    _reset()
    m._clear_login_failures("nobody", "1.2.3.4")
    assert ("nobody", "1.2.3.4") not in m._LOGIN_ATTEMPTS_USER
    assert "1.2.3.4" not in m._LOGIN_ATTEMPTS_IP
    _step("no crash, no state created")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
TESTS = [
    test_nat_collateral_lockout_fix,
    test_attacker_residue_remains,
    test_eviction_cascade_no_orphans,
    test_nat_eviction_partial_recovery,
    test_429_logging_no_username_leak,
    test_x_forwarded_for_parsing,
    test_no_op_clear_path,
]


def main() -> int:
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
