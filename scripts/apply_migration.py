"""Apply one SQL migration to the Supabase project via the Management API.

Replaces the one-off apply_003 / apply_003a / apply_004 scripts. Given a
migration number it sends `invoiceflow/migrations/<NNN>_*.sql` as a single
query (the files wrap themselves in BEGIN/COMMIT), then runs every
statement in the matching `<NNN>_verify.sql` so the result can be checked
against the "Expected:" header of that file.

Needs a Supabase personal access token (Dashboard → Account → Access
Tokens) in SUPABASE_PAT. The project ref is taken from SUPABASE_PROJECT_REF,
or derived from SUPABASE_URL (https://<ref>.supabase.co) in the
environment / invoiceflow/.env.

Usage
-----
  # show what would be sent (writes nothing)
  SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006 --dry-run

  # apply + verify
  SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006

  # run only the verify queries of an already-applied migration
  SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006 --verify-only

  # roll back (sends <NNN>_*_rollback.sql instead)
  SUPABASE_PAT=sbp_... python scripts/apply_migration.py 006 --rollback
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
MIG_DIR = HERE.parent / "invoiceflow" / "migrations"
load_dotenv(HERE.parent / "invoiceflow" / ".env")


def _project_ref() -> str:
    ref = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    if ref:
        return ref
    m = re.match(r"https?://([a-z0-9-]+)\.supabase\.co", os.environ.get("SUPABASE_URL", ""))
    if m:
        return m.group(1)
    sys.exit("ERROR: set SUPABASE_PROJECT_REF (or SUPABASE_URL) so the project can be identified")


def _find(number: str, suffix: str) -> Path | None:
    """The single migrations/<number>_*<suffix>.sql file, or None."""
    # Forward + rollback files carry a name part (006_add_x.sql); the verify
    # file is bare (006_verify.sql). Anchor on the number so 003 never
    # matches 003a.
    pat = re.compile(rf"{re.escape(number)}(_.*)?{re.escape(suffix)}\.sql")
    hits = [p for p in MIG_DIR.glob(f"{number}*.sql") if pat.fullmatch(p.name)]
    if suffix == "":  # the forward migration: exclude _rollback / _verify
        hits = [p for p in hits if not p.name.endswith(("_rollback.sql", "_verify.sql"))]
    if len(hits) > 1:
        sys.exit(f"ERROR: ambiguous migration {number!r}: {[p.name for p in hits]}")
    return hits[0] if hits else None


def _statements(sql: str) -> list[str]:
    """Split a verify file into its statements (comments stripped)."""
    body = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def run_sql(api: str, pat: str, query: str, label: str) -> None:
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        api,
        data=payload,
        headers={
            "Authorization": f"Bearer {pat}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    print(f"\n=== {label} ===")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(json.dumps(json.loads(resp.read().decode("utf-8")), indent=2))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("number", help="migration number, e.g. 006 (or 003a)")
    ap.add_argument("--rollback", action="store_true", help="send the _rollback file instead")
    ap.add_argument("--verify-only", action="store_true", help="skip apply, run the _verify queries")
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")
    args = ap.parse_args()

    forward = _find(args.number, "_rollback" if args.rollback else "")
    verify = None if args.rollback else _find(args.number, "_verify")
    if not args.verify_only and forward is None:
        sys.exit(f"ERROR: no migration {args.number!r} in {MIG_DIR}")
    if args.verify_only and verify is None:
        sys.exit(f"ERROR: no {args.number}_verify.sql in {MIG_DIR}")

    steps: list[tuple[str, str]] = []
    if not args.verify_only:
        steps.append((f"APPLY {forward.name}", forward.read_text(encoding="utf-8")))
    if verify is not None:
        for i, stmt in enumerate(_statements(verify.read_text(encoding="utf-8")), 1):
            steps.append((f"VERIFY {i} ({verify.name})", stmt))

    if args.dry_run:
        for label, sql in steps:
            print(f"\n=== {label} ===\n{sql}")
        print(f"\n[dry-run] {len(steps)} request(s) would be sent; nothing written.")
        return

    pat = os.environ.get("SUPABASE_PAT")
    if not pat:
        sys.exit("ERROR: SUPABASE_PAT env var not set")
    api = f"https://api.supabase.com/v1/projects/{_project_ref()}/database/query"
    for label, sql in steps:
        run_sql(api, pat, sql, label)
    if verify is not None:
        print(f"\nCompare the VERIFY output with the 'Expected:' header of {verify.name}.")
    print("\n[done]")


if __name__ == "__main__":
    main()
