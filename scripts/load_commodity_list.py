"""Load (or refresh) the company-wide commodity-code list (the "V-lookup").

The codes come from a MultiFreight CDS "Items" export — every goods line
actually declared, so the list is the codes as accepted at the border. The
export is folded into one row per (exporter REX, full code) by
`invoiceflow/cds_list.py` and committed as `invoiceflow/data/commodity_codes.csv`;
this script merges those rows to ONE row per full code (most clients use the
same codes, so the list is shared by all of them) and upserts them into
`commodity_codes`.

Nothing is ever deleted: rows are upserted on (company_id, full_code), so
re-running is safe and the list keeps any code that predates the export
(unless --replace is asked for explicitly).

The exporter REX numbers found in the file are still used — not to pick a
list, but to keep the CLIENTS registry fresh: each known REX gets its
client's identity (name/EORI/aliases) re-asserted, and unknown REX numbers
are reported so the operator can add the client in the app (Clients / REX
tool). The registry feeds invoice matching and the export's REX fallback.

Usage
-----
  # what is in the list file, and which REX numbers already have a client
  python scripts/load_commodity_list.py --list

  # refresh the company list from the committed CSV (the usual run)
  python scripts/load_commodity_list.py

  # re-derive the committed list from a new export first
  python scripts/load_commodity_list.py --source "C:/Users/Beverley/Downloads/CDS items.csv" --derive

  # start over: wipe the list, then load
  python scripts/load_commodity_list.py --replace

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
    p.add_argument("--replace", action="store_true",
                   help="delete the company's existing list before loading")
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


def refresh_client_registry(cid: str, rex_numbers: list[str],
                            *, dry_run: bool) -> None:
    """Keep the clients registry (Clients / REX tool) in step with the file.

    A client is only created for a REX in KNOWN_CLIENTS — auto-creating one
    per unseen REX would fill the registry with nameless rows. Unknown REX
    numbers are reported so the operator can add them in the app.
    """
    unknown = []
    for rex in sorted(r for r in rex_numbers if r):
        known = KNOWN_CLIENTS.get(rex, {})
        existing = db.find_client_by_identity(cid, rex=rex,
                                              eori=known.get("eori", ""))
        if existing:
            if known and not dry_run:
                db.update_client(cid, existing["id"], {
                    "rex": rex,
                    "eori": known.get("eori"),
                    "aliases": known.get("aliases", []),
                })
            continue
        if known:
            if not dry_run:
                client = db.get_or_create_client(
                    cid, known["name"], rex=rex, eori=known.get("eori", ""))
                if known.get("aliases"):
                    db.update_client(cid, client["id"],
                                     {"aliases": known["aliases"]})
            print(f"  registry: created client {known['name']!r} [{rex}]")
        else:
            unknown.append(rex)
    if unknown:
        print(f"{len(unknown)} REX numbers have no client in the registry — "
              f"add them in the app (Clients / REX tool) so invoices match:")
        for rex in unknown:
            print(f"  {rex}")


def spot_check(cid: str, entries: list[dict], limit: int = 3) -> None:
    """Print a few VLOOKUPs straight back out of the DB, as proof of load."""
    print("\nSpot-check the V-lookup:")
    for entry in entries[:limit]:
        for hit in db.get_commodity_codes_by_general_code(
                cid, entry["general_code"]):
            print(f"  {entry['general_code']} -> {hit['full_code']}  "
                  f"{hit['description']}")
    print(f"  list now holds {db.count_commodity_codes(cid)} codes")


def main():
    args = parse_args()
    entries = load_entries(args)
    by_rex = cds_list.group_by_rex(entries)

    if args.show:
        comp = db.get_company_by_name(COMPANY_NAME)
        cid = comp["id"] if comp else None
        merged = cds_list.merge_by_code(entries)
        no_rex = len(by_rex.get("", []))
        print(f"\n{len(merged)} distinct codes "
              f"({len(entries)} entries across "
              f"{len(by_rex) - (1 if '' in by_rex else 0)} REX numbers, "
              f"{no_rex} codes on lines with no REX).")
        rows = sorted(((len(v), k) for k, v in by_rex.items() if k), reverse=True)
        print(f"\n{'REX':24s} {'codes':>6s}  client in registry")
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

    # ── One shared list: fold every REX group to one row per full code ──
    merged = cds_list.merge_by_code(entries)
    if args.replace and not args.dry_run:
        db.delete_commodity_codes(cid)
    if not args.dry_run:
        for entry in merged:
            db.upsert_commodity_code(cid, cds_list.product_payload(entry))
    print(f"{len(merged)} codes "
          f"({'dry run' if args.dry_run else 'written'}).")

    refresh_client_registry(cid, list(by_rex.keys()), dry_run=args.dry_run)

    if not args.dry_run:
        spot_check(cid, merged)


if __name__ == "__main__":
    main()
