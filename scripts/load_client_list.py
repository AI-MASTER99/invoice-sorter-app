"""Load (or refresh) client commodity-code lists (the "V-lookup") into the DB.

The codes come from a MultiFreight CDS "Items" export — every goods line
actually declared, so the list is the codes as accepted at the border. The
export is folded into one row per (exporter REX, full code) by
`invoiceflow/cds_list.py` and committed as `invoiceflow/data/commodity_codes.csv`;
this script upserts those rows into `client_products`.

Nothing is ever deleted: rows are upserted on (company_id, client_id,
full_code), so re-running is safe and a list keeps any code that predates
the export (unless --replace is asked for explicitly).

Which client a code belongs to comes from the exporter's REX in the line's
U116 proof-of-origin document — the same identity the app matches invoices
on. Lines that declared no U116 (non-EU origin) carry no REX and are only
loaded when a client is named explicitly with --all-codes.

Usage
-----
  # what is in the list file, and which REX numbers already have a client
  python scripts/load_client_list.py --list

  # refresh every client that already exists in the DB (the usual run)
  python scripts/load_client_list.py

  # one client, by REX — creating it if this is its first list
  python scripts/load_client_list.py --rex ITREXIT06167560157 --name "Lorenzo Apicella"

  # give one client every code in the file, REX or not
  python scripts/load_client_list.py --client "Lorenzo Apicella" --all-codes

  # re-derive the committed list from a new export first
  python scripts/load_client_list.py --source "C:/Users/Beverley/Downloads/CDS items.csv" --derive

Add --dry-run to any of them to see the counts without writing.
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "invoiceflow" / ".env")
sys.path.insert(0, str(ROOT / "invoiceflow"))
import cds_list                       # noqa: E402
import database as db                 # noqa: E402

# ── Config ───────────────────────────────────────────────────────────
COMPANY_NAME = "Dornack"
DERIVED_CSV = ROOT / "invoiceflow" / "data" / "commodity_codes.csv"

# The first client, kept here so its identity (aliases/EORI) is restored on
# every run — the REX is what the export and the invoice matcher agree on.
KNOWN_CLIENTS = {
    "ITREXIT06167560157": {
        "name": "Lorenzo Apicella",
        "eori": "IT 06167560157",
        "aliases": ["APICELLA LORENZO S.A.S.", "APICELLA LORENZO", "APICELLA"],
    },
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", help="raw CDS Items export (CSV) to read instead "
                                    "of the committed list")
    p.add_argument("--derive", action="store_true",
                   help="rewrite invoiceflow/data/commodity_codes.csv from --source")
    p.add_argument("--rex", help="load only this exporter REX")
    p.add_argument("--client", help="target client by name (with --all-codes, or "
                                    "to redirect a --rex group)")
    p.add_argument("--name", help="client name to use when --rex creates a client")
    p.add_argument("--all-codes", action="store_true",
                   help="load EVERY code in the file (including the lines with no "
                        "REX) into the --client list")
    p.add_argument("--replace", action="store_true",
                   help="delete a client's existing list before loading")
    p.add_argument("--list", action="store_true", dest="show",
                   help="report the file's contents and stop")
    p.add_argument("--dry-run", action="store_true", help="write nothing")
    return p.parse_args()


def load_entries(args) -> list[dict]:
    """The list rows: from a raw export when --source, else the committed CSV."""
    if args.source:
        entries, stats = cds_list.parse_export(args.source)
        print(f"Read {stats['rows']} export lines "
              f"({stats['skipped_no_code']} without a usable code) -> "
              f"{stats['entries']} list entries, {stats['clients']} REX numbers.")
        if args.derive and not args.dry_run:
            cds_list.write_derived(entries, DERIVED_CSV)
            print(f"Wrote {DERIVED_CSV.relative_to(ROOT)}")
        return entries
    if not DERIVED_CSV.exists():
        raise SystemExit(f"{DERIVED_CSV} not found — pass --source <export.csv>")
    entries = cds_list.read_derived(DERIVED_CSV)
    print(f"Read {len(entries)} list entries from "
          f"{DERIVED_CSV.relative_to(ROOT)}")
    return entries


def upsert_entries(cid: str, client: dict, entries: list[dict],
                   *, replace: bool, dry_run: bool) -> int:
    """Upsert one client's list rows. Returns how many were written."""
    clid = client["id"]
    if dry_run:
        return len(entries)
    if replace:
        db.delete_client_products(cid, clid)
    for entry in entries:
        db.upsert_client_product(cid, clid, cds_list.product_payload(entry))
    return len(entries)


def resolve_client(cid: str, rex: str, name: str = "") -> dict | None:
    """The DB client for a REX — never created implicitly.

    A client is only created when the caller supplies a name (--name, or
    KNOWN_CLIENTS), because the export carries no company names: auto-creating
    one client per unseen REX would fill the editor with 1000+ nameless rows.
    """
    known = KNOWN_CLIENTS.get(rex, {})
    existing = db.find_client_by_identity(cid, rex=rex, eori=known.get("eori", ""))
    if existing:
        if known:
            db.update_client(cid, existing["id"], {
                "rex": rex,
                "eori": known.get("eori"),
                "aliases": known.get("aliases", []),
            })
        return existing
    label = name or known.get("name")
    if not label:
        return None
    client = db.get_or_create_client(cid, label, rex=rex, eori=known.get("eori", ""))
    if known.get("aliases"):
        db.update_client(cid, client["id"], {"aliases": known["aliases"]})
    return client


def spot_check(cid: str, client: dict, entries: list[dict], limit: int = 3) -> None:
    """Print a few VLOOKUPs straight back out of the DB, as proof of load."""
    print(f"\nSpot-check the V-lookup for {client['name']}:")
    for entry in entries[:limit]:
        for hit in db.get_client_products_by_general_code(
                cid, client["id"], entry["general_code"]):
            print(f"  {entry['general_code']} -> {hit['full_code']}  "
                  f"{hit['description']}")
    print(f"  list now holds {db.count_client_products(cid, client['id'])} codes")


def main():
    args = parse_args()
    entries = load_entries(args)
    by_rex = cds_list.group_by_rex(entries)

    if args.show:
        comp = db.get_company_by_name(COMPANY_NAME)
        cid = comp["id"] if comp else None
        no_rex = len(by_rex.get("", []))
        print(f"\n{len(by_rex) - (1 if '' in by_rex else 0)} REX numbers, "
              f"{no_rex} codes on lines with no REX.")
        rows = sorted(((len(v), k) for k, v in by_rex.items() if k), reverse=True)
        print(f"\n{'REX':24s} {'codes':>6s}  client in DB")
        for n, rex in rows[:40]:
            client = (db.find_client_by_identity(cid, rex=rex) if cid else None)
            print(f"{rex:24s} {n:6d}  {client['name'] if client else '—'}")
        if len(rows) > 40:
            print(f"… and {len(rows) - 40} more REX numbers")
        return

    comp = db.get_company_by_name(COMPANY_NAME)
    if not comp:
        raise SystemExit(f"Company {COMPANY_NAME!r} not found")
    cid = comp["id"]

    # ── Target selection ─────────────────────────────────────────────
    if args.all_codes:
        if not args.client:
            raise SystemExit("--all-codes needs --client \"<name>\"")
        client = db.get_or_create_client(cid, args.client)
        merged = cds_list.merge_by_code(entries)
        written = upsert_entries(cid, client, merged,
                                 replace=args.replace, dry_run=args.dry_run)
        print(f"{client['name']}: {written} codes "
              f"({'dry run' if args.dry_run else 'written'})")
        if not args.dry_run:
            spot_check(cid, client, merged)
    elif args.rex:
        rex = args.rex.strip().upper()
        group = by_rex.get(rex)
        if not group:
            raise SystemExit(f"No codes in the list for REX {rex}")
        client = (db.get_or_create_client(cid, args.client) if args.client
                  else resolve_client(cid, rex, args.name or ""))
        if not client:
            raise SystemExit(
                f"REX {rex} has no client in the DB. Re-run with "
                f"--name \"<client name>\" to create one.")
        written = upsert_entries(cid, client, group,
                                 replace=args.replace, dry_run=args.dry_run)
        print(f"{client['name']} [{rex}]: {written} codes "
              f"({'dry run' if args.dry_run else 'written'})")
        if not args.dry_run:
            spot_check(cid, client, group)
    else:
        # The usual run: refresh every REX that already has a client.
        total, touched, skipped = 0, 0, []
        for rex, group in sorted(by_rex.items()):
            if not rex:
                continue
            client = resolve_client(cid, rex)
            if not client:
                skipped.append((rex, len(group)))
                continue
            written = upsert_entries(cid, client, group,
                                     replace=args.replace, dry_run=args.dry_run)
            total += written
            touched += 1
            print(f"  {client['name']:32s} [{rex}] {written:4d} codes")
        print(f"\n{total} codes across {touched} clients "
              f"({'dry run' if args.dry_run else 'written'}).")
        if skipped:
            unclaimed = sum(n for _, n in skipped)
            print(f"{len(skipped)} REX numbers ({unclaimed} codes) have no client "
                  f"in the DB — add the client, then re-run with "
                  f"--rex <REX> --name \"<client name>\".")
        if by_rex.get(""):
            print(f"{len(by_rex[''])} codes came from lines with no REX "
                  f"(non-EU origin). Load them with "
                  f"--client \"<name>\" --all-codes if a client needs them.")


if __name__ == "__main__":
    main()
