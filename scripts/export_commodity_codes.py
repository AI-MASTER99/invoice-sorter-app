"""Write the commodity-code list out as a spreadsheet, ready to re-import.

The app's Commodity codes tool imports a sheet of codes; this produces one
from the committed list, so the whole list can be pushed into a database
that is missing codes or holding blank rows. (The rows migration 006
seeded from the per-client lists arrived with no description and no TARIC;
importing this sheet fills them, because the import fills a blank field
and leaves a filled one alone.)

The sheet is the shape `cds_list.read_code_list` reads back:

    Commodity Code | Additional Taric | Description
    02013000       | 90               | TARTARE DI FASONA

Codes are written as TEXT so chapters 01-09 keep the leading zero a
spreadsheet would otherwise eat (02013000 -> 2013000).

Usage
-----
  python scripts/export_commodity_codes.py
  python scripts/export_commodity_codes.py --out /tmp/codes.xlsx
"""
import argparse
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "invoiceflow"))
import cds_list                       # noqa: E402

DERIVED_CSV = ROOT / "invoiceflow" / "data" / "commodity_codes.csv"
DEFAULT_OUT = ROOT / "Commodity_Codes_Full_List.xlsx"
COLUMNS = ["Commodity Code", "Additional Taric", "Description"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default=str(DERIVED_CSV),
                   help="the list to export (default: the committed CSV)")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help=f"where to write it (default: {DEFAULT_OUT.name})")
    return p.parse_args()


def main():
    args = parse_args()
    src = Path(args.source)
    if not src.exists():
        raise SystemExit(f"{src} not found")

    # One row per code — the same fold the loader applies before writing.
    entries = cds_list.merge_by_code(cds_list.read_derived(src))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Commodity Codes"
    ws.append(COLUMNS)
    for e in entries:
        ws.append([e["general_code"], e["taric_code"], e["description"]])
    for col, width in zip("ABC", (18, 16, 70)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2, max_col=2):
        for cell in row:
            cell.number_format = "@"          # text, so 0201 stays 0201

    out = Path(args.out)
    wb.save(out)
    print(f"Wrote {out} — {len(entries)} codes.\n"
          f"Import it in the app (Commodity codes -> Import list) to fill a "
          f"database that is missing codes or holding blank rows.")


if __name__ == "__main__":
    main()
