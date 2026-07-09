"""Bulk-clean Supabase Storage to get back under the free-tier quota.

The app uploads two objects per processed invoice — the original file to
`invoice-uploads` and the two generated spreadsheets to `invoice-exports`
— and never deletes them unless a user manually removes the invoice. Over
months this fills the bucket (20 GB seen in production, free tier caps at
5 GB → the project gets restricted and the whole app goes down).

This script lists every object in both buckets, reports how much space
each is using, and deletes the ones older than a cutoff. It is a
DRY-RUN by default: it shows exactly what it *would* delete and the space
that would be freed. Add --apply to actually delete.

Deleting a stored file does NOT remove its `invoices` row — the row is a
few hundred bytes and keeps the invoice visible in the app; only its
download links go dead (expected for archived invoices). The 20 GB is the
files, so freeing them is what un-restricts the project.

Usage:
    # from repo root, with the service-role key available:
    SUPABASE_URL=... SUPABASE_KEY=... python scripts/storage_cleanup.py
    # or let it read invoiceflow/.env automatically

    python scripts/storage_cleanup.py                 # dry-run, >30 days old
    python scripts/storage_cleanup.py --older-than 14 # dry-run, >14 days old
    python scripts/storage_cleanup.py --older-than 30 --apply   # DELETE
    python scripts/storage_cleanup.py --bucket invoice-exports --older-than 0 --apply

Notes:
  - --older-than 0 targets EVERY file (use with care).
  - Deletes go through the service-role key; they still work while the
    project is over-quota (deleting reduces usage). If the project is
    fully *paused*, resume/restore it first (Supabase dashboard) so the
    Storage API responds, then run this.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / "invoiceflow" / ".env"

BUCKETS = ("invoice-uploads", "invoice-exports")
_LIST_PAGE = 1000  # Supabase storage list page size


def _load_env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if (not url or not key) and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "SUPABASE_URL" and not url:
                url = v
            elif k == "SUPABASE_KEY" and not key:
                key = v
    if not url or not key:
        sys.exit(
            "ERROR: SUPABASE_URL and SUPABASE_KEY (service-role) must be set,\n"
            f"       either in the environment or in {ENV_FILE}"
        )
    return url, key


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _parse_created(entry: dict) -> datetime | None:
    raw = entry.get("created_at") or entry.get("updated_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _list_folder(store, prefix: str) -> list[dict]:
    """Return every entry (files + subfolders) under prefix, paginated."""
    out: list[dict] = []
    offset = 0
    while True:
        page = store.list(prefix, {"limit": _LIST_PAGE, "offset": offset})
        if not page:
            break
        out.extend(page)
        if len(page) < _LIST_PAGE:
            break
        offset += _LIST_PAGE
    return out


def _walk(store, prefix: str = "") -> list[dict]:
    """Recursively collect file objects (not folders) with full paths."""
    files: list[dict] = []
    for entry in _list_folder(store, prefix):
        name = entry.get("name")
        if not name:
            continue
        full = f"{prefix}/{name}" if prefix else name
        # A folder placeholder has no size metadata; recurse into it.
        meta = entry.get("metadata")
        if meta and isinstance(meta, dict) and meta.get("size") is not None:
            files.append({
                "path": full,
                "size": int(meta.get("size") or 0),
                "created": _parse_created(entry),
            })
        else:
            files.extend(_walk(store, full))
    return files


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-clean Supabase Storage buckets.")
    ap.add_argument("--older-than", type=int, default=30,
                    help="Delete files older than N days (0 = all). Default 30.")
    ap.add_argument("--bucket", choices=BUCKETS, default=None,
                    help="Limit to one bucket. Default: both.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Without this it is a dry-run.")
    args = ap.parse_args()

    url, key = _load_env()
    from supabase import create_client
    sb = create_client(url, key)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    buckets = (args.bucket,) if args.bucket else BUCKETS

    grand_total = 0
    grand_del = 0
    grand_del_bytes = 0

    for bucket in buckets:
        store = sb.storage.from_(bucket)
        try:
            files = _walk(store)
        except Exception as e:  # noqa: BLE001
            print(f"! {bucket}: could not list ({type(e).__name__}: {e})")
            continue

        total_bytes = sum(f["size"] for f in files)
        grand_total += total_bytes

        # A file with no timestamp is treated as old (safer to include when
        # the goal is freeing space and older-than is explicit).
        victims = [
            f for f in files
            if args.older_than == 0 or (f["created"] or cutoff) <= cutoff
        ]
        victim_bytes = sum(f["size"] for f in victims)

        print(f"\n=== {bucket} ===")
        print(f"  files: {len(files):>6}   size: {_human(total_bytes)}")
        print(f"  matching (>{args.older_than}d): {len(victims):>6}   "
              f"would free: {_human(victim_bytes)}")

        if not victims:
            continue

        if not args.apply:
            grand_del += len(victims)
            grand_del_bytes += victim_bytes
            for f in victims[:5]:
                when = f["created"].date().isoformat() if f["created"] else "?"
                print(f"    would delete  {when}  {_human(f['size']):>9}  {f['path']}")
            if len(victims) > 5:
                print(f"    … and {len(victims) - 5} more")
            continue

        # Apply: delete in batches.
        paths = [f["path"] for f in victims]
        deleted = 0
        BATCH = 100
        for i in range(0, len(paths), BATCH):
            chunk = paths[i:i + BATCH]
            try:
                store.remove(chunk)
                deleted += len(chunk)
                print(f"    deleted {deleted}/{len(paths)} …")
            except Exception as e:  # noqa: BLE001
                print(f"    ! batch failed ({type(e).__name__}: {e})")
        grand_del += deleted
        grand_del_bytes += victim_bytes

    print("\n" + "=" * 40)
    print(f"Total storage seen:   {_human(grand_total)}")
    verb = "Deleted" if args.apply else "Would delete"
    print(f"{verb}:            {grand_del} files  ({_human(grand_del_bytes)})")
    if not args.apply:
        print("\nDRY-RUN — nothing was deleted. Re-run with --apply to delete.")


if __name__ == "__main__":
    main()
