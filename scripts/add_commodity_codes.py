"""Merge a plain commodity-code list into the committed list, without duplicates.

`load_commodity_list.py` derives the list from a MultiFreight CDS "Items"
export — every code actually declared. This script is for the other source:
a spreadsheet of codes (one row per code) that someone keeps by hand or
exports from elsewhere, and wants folded into the same list.

    Commodity Code | Additional Taric | Description
    02013000       | 90               | TARTARE DI FASONA

Only codes the list does not already hold are added. A code already present
under ANY exporter REX is already in the company-wide list, so it is left
exactly as it is — its wording is backed by real declared lines, which a
sheet's wording is not, and nothing is ever written twice. Re-running the
same sheet therefore adds nothing the second time.

The merged list is written back to `invoiceflow/data/commodity_codes.csv`;
run `load_commodity_list.py` afterwards to push it into the database.

Usage
-----
  # what the sheet would add (writes nothing)
  python scripts/add_commodity_codes.py "Commodity_Codes_2026.xlsx" --dry-run

  # merge it in
  python scripts/add_commodity_codes.py "Commodity_Codes_2026.xlsx"

  # then load the refreshed list into the DB
  python scripts/load_commodity_list.py
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "invoiceflow"))
import cds_list                       # noqa: E402

DERIVED_CSV = ROOT / "invoiceflow" / "data" / "commodity_codes.csv"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", help="the code list to merge in (.xlsx or .csv)")
    p.add_argument("--into", default=str(DERIVED_CSV),
                   help="the list to merge into (default: the committed CSV)")
    p.add_argument("--dry-run", action="store_true", help="write nothing")
    p.add_argument("--show", type=int, default=20, metavar="N",
                   help="how many added codes to print (default 20)")
    return p.parse_args()


def main():
    args = parse_args()
    into = Path(args.into)
    if not into.exists():
        raise SystemExit(f"{into} not found")

    existing = cds_list.read_derived(into)
    additions, read_stats = cds_list.read_code_list(args.source)
    print(f"Read {read_stats['rows']} rows from {Path(args.source).name} -> "
          f"{read_stats['entries']} codes "
          f"({read_stats['skipped_no_code']} without a usable code).")
    for code, taric, desc in read_stats["skipped"]:
        print(f"  skipped {code!r}/{taric!r}  {desc}  — not a full 8-digit code")

    had = {e["full_code"] for e in existing}
    merged, stats = cds_list.add_entries(existing, additions)
    print(f"\n{stats['already_present']} of those codes were already in the "
          f"list (left as they are), {stats['added']} new.")
    for entry in [e for e in additions if e["full_code"] not in had][:args.show]:
        print(f"  + {entry['full_code']}  {entry['description']}")
    if stats["added"] > args.show:
        print(f"  … and {stats['added'] - args.show} more")

    if not stats["added"]:
        print(f"\nNothing to add — {into.name} is unchanged "
              f"({len(existing)} entries, "
              f"{len({e['full_code'] for e in existing})} distinct codes).")
        return
    if args.dry_run:
        print(f"\nDry run — {into.name} not written.")
        return
    cds_list.write_derived(merged, into)
    print(f"\nWrote {len(merged)} entries "
          f"({len({e['full_code'] for e in merged})} distinct codes) to "
          f"{into.relative_to(ROOT) if into.is_relative_to(ROOT) else into}.\n"
          f"Run scripts/load_commodity_list.py to load them into the DB.")


if __name__ == "__main__":
    main()
