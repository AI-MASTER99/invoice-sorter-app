"""Load (or refresh) a client + its commodity-code list into the DB.

Configured here for the first client, APICELLA, from the
'AI EURO CODE TARIC DESCRIPTION.xlsx' list (columns: A=general code,
B=2-digit suffix, C=description). Re-running is safe: clients are matched
by REX and products are upserted on (company_id, client_id, full_code).

Usage: python scripts/load_client_list.py
"""
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "invoiceflow" / ".env")
sys.path.insert(0, str(ROOT / "invoiceflow"))
import database as db
import openpyxl

# ── Config for this client ───────────────────────────────────────────
COMPANY_NAME = "Dornack"
CLIENT = {
    "name": "Lorenzo Apicella",
    "rex": "ITREXIT06167560157",
    "eori": "IT 06167560157",
    "aliases": ["APICELLA LORENZO S.A.S.", "APICELLA LORENZO", "APICELLA"],
}
LIST_XLSX = r"C:/Users/Beverley/Downloads/AI EURO CODE TARIC DESCRIPTION.xlsx"


def norm_digits(v, width):
    s = re.sub(r"\D", "", str(v or ""))
    return s.zfill(width) if s else ""


def main():
    comp = db.get_company_by_name(COMPANY_NAME)
    if not comp:
        raise SystemExit(f"Company {COMPANY_NAME!r} not found")
    cid = comp["id"]

    client = db.get_or_create_client(
        cid, CLIENT["name"], rex=CLIENT["rex"], eori=CLIENT["eori"])
    clid = client["id"]
    # Make sure aliases/eori are stored even if the client already existed.
    db.update_client(cid, clid, {"aliases": CLIENT["aliases"], "eori": CLIENT["eori"]})
    print(f"Client: {client['name']}  ({clid})")

    wb = openpyxl.load_workbook(LIST_XLSX, data_only=True)
    ws = wb.active
    loaded = 0
    for row in ws.iter_rows(values_only=True):
        if not row or row[0] in (None, ""):
            continue
        general = norm_digits(row[0], 8)
        suffix = norm_digits(row[1] if len(row) > 1 else "", 2) or "00"
        desc = str(row[2]).strip() if len(row) > 2 and row[2] not in (None, "") else ""
        if not general:
            continue
        full = general + suffix
        db.upsert_client_product(cid, clid, {
            "general_code": general,
            "full_code": full,
            "description": desc,
        })
        loaded += 1

    total = db.count_client_products(cid, clid)
    print(f"Upserted {loaded} rows. Client now has {total} products.")
    print("\nSample VLOOKUPs:")
    for code in ("04061030", "07020010", "07099910"):
        hits = db.get_client_products_by_general_code(cid, clid, code)
        for h in hits:
            print(f"  {code} -> {h['full_code']}  {h['description']}")


if __name__ == "__main__":
    main()
