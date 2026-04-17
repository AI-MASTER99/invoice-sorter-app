"""
Invoice Sorter — FastAPI backend
Processes supplier invoices (PDF/JPG/DOCX) via Claude API,
runs dual-verification, looks up UK Trade Tariff, exports to Excel.
"""

import asyncio
import base64
import io
import json
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

from datetime import date, datetime
from typing import Any

import anthropic
import httpx
import openpyxl
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

# Database layer (Supabase)
import database as db

# ---------------------------------------------------------------------------
# Paths & startup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent

# Op Vercel is het filesystem read-only; gebruik /tmp voor schrijfbare mappen
_ON_VERCEL = os.environ.get("VERCEL") == "1"
if _ON_VERCEL:
    _DATA_ROOT = Path("/tmp/invoice-sorter")
else:
    _DATA_ROOT = BASE_DIR

UPLOADS_DIR = _DATA_ROOT / "uploads"
OUTPUT_DIR  = _DATA_ROOT / "output"

# AI models — the primary one does the heavy lifting (extraction, verification,
# sub-code matching — anything where quality directly affects customs outcomes).
# The light one handles the simple totals extraction where Sonnet is plenty.
AI_MODEL_PRIMARY = os.environ.get("AI_MODEL_PRIMARY", "claude-opus-4-7")
AI_MODEL_LIGHT   = os.environ.get("AI_MODEL_LIGHT",   "claude-sonnet-4-6")

# Legacy single-model env var — if set, override both (for easy rollback)
if os.environ.get("AI_MODEL"):
    AI_MODEL_PRIMARY = os.environ["AI_MODEL"]
    AI_MODEL_LIGHT   = os.environ["AI_MODEL"]

for d in (UPLOADS_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Auth helpers (Supabase-backed, multi-tenant)
# ---------------------------------------------------------------------------
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def ensure_default_admin():
    """Make sure the default admin user exists with a correct password hash.
    Runs once at startup — idempotent."""
    existing = db.get_user("admin", db.DEFAULT_COMPANY_ID)
    default_pw = os.environ.get("APP_PASSWORD", "changeme")
    new_hash = _pwd_ctx.hash(default_pw)
    if not existing:
        db.create_user(db.DEFAULT_COMPANY_ID, "admin", new_hash, "admin")
    elif existing.get("password_hash", "").startswith("$2b$12$placeholder"):
        # Schema's seed placeholder — replace with real hash
        db.update_user_password(existing["id"], new_hash)


ensure_default_admin()


def require_auth(request: Request) -> dict:
    """Returns {user_id, username, company_id, role}."""
    sess_user_id = request.session.get("user_id")
    if not sess_user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "user_id":    sess_user_id,
        "username":   request.session.get("username", ""),
        "company_id": request.session.get("company_id", ""),
        "role":       request.session.get("role", "user"),
    }


def require_admin(request: Request) -> dict:
    """Admin OR super_admin can access."""
    ctx = require_auth(request)
    if ctx["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin required")
    return ctx


def require_super_admin(request: Request) -> dict:
    """Only super_admin — manages all companies."""
    ctx = require_auth(request)
    if ctx["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin required")
    return ctx

# ---------------------------------------------------------------------------
# Threading lock (retained for in-process safety, data lives in Supabase)
# ---------------------------------------------------------------------------
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sequential job queue — processes one invoice at a time to respect rate limits
# Queue carries (job_id, company_id, file_path, original_name, mime, upload_storage_path)
# ---------------------------------------------------------------------------
_job_queue: queue.Queue[tuple[str, str, Path, str, str, str]] = queue.Queue()


def _queue_worker():
    """Single worker thread that processes jobs from the queue one at a time."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        job_id, company_id, file_path, original_name, mime, upload_storage_path = _job_queue.get()
        try:
            db.update_job(job_id, {"status": "running", "step": "Starting…"})
        except Exception:
            pass
        try:
            loop.run_until_complete(
                _process_invoice(job_id, company_id, file_path, original_name, mime, upload_storage_path)
            )
        except Exception:
            pass  # _process_invoice handles its own errors
        finally:
            _job_queue.task_done()


# Start the single worker thread at module load
_worker_thread = threading.Thread(target=_queue_worker, daemon=True)
_worker_thread.start()


def _enqueue_job(job_id: str, company_id: str, file_path: Path, original_name: str, mime: str, upload_storage_path: str = ""):
    """Add a job to the processing queue."""
    _job_queue.put((job_id, company_id, file_path, original_name, mime, upload_storage_path))


# ---------------------------------------------------------------------------
# Tariff lookup
# ---------------------------------------------------------------------------
def _extract_duty_vat(data: dict) -> tuple[str, str]:
    """Extract duty and VAT strings from a UK Tariff API commodity response.

    The API stores duty rates in separate 'duty_expression' items linked to
    'measure' items via their ID (e.g. measure id "20237627" links to
    duty_expression id "20237627-duty_expression").
    """
    included = data.get("included", [])
    duty = ""
    vat = ""

    # Build lookup: duty_expression id → base string
    duty_exprs: dict[str, str] = {}
    for item in included:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "duty_expression":
            attrs = item.get("attributes", {})
            base = attrs.get("base", "") or ""
            base = re.sub(r"<[^>]+>", "", base).strip()
            if base:
                duty_exprs[item.get("id", "")] = base

    # Find measures and look up their duty expressions
    for item in included:
        if not isinstance(item, dict) or item.get("type") != "measure":
            continue
        attrs = item.get("attributes", {})
        # measure_type_description is sometimes nested differently
        mtype = ""
        # Try direct attribute first
        mtype = attrs.get("measure_type_description", "")
        # Also check relationships for measure_type
        if not mtype:
            rels = item.get("relationships", {})
            mt_data = rels.get("measure_type", {}).get("data", {})
            mt_id = mt_data.get("id", "") if isinstance(mt_data, dict) else ""
            # Look up measure_type in included
            for inc in included:
                if inc.get("type") == "measure_type" and inc.get("id") == mt_id:
                    mtype = inc.get("attributes", {}).get("description", "")
                    break

        # Find linked duty_expression
        measure_id = item.get("id", "")
        expr_key = f"{measure_id}-duty_expression"
        expr_base = duty_exprs.get(expr_key, "")

        # Also check inline duty_expression attribute
        if not expr_base:
            inline = attrs.get("duty_expression", {})
            if isinstance(inline, dict):
                expr_base = re.sub(r"<[^>]+>", "", inline.get("base", "") or "").strip()

        if "Third country duty" in mtype and not duty and expr_base:
            duty = expr_base
        if "VAT" in mtype and not vat:
            if expr_base:
                vat = expr_base

    return duty, vat


def _extract_commodity_desc(data: dict, code: str) -> str:
    """Extract the commodity description from included items."""
    for item in data.get("included", []):
        if not isinstance(item, dict) or item.get("type") != "commodity":
            continue
        attrs = item.get("attributes", {})
        if attrs.get("goods_nomenclature_item_id") == code:
            return re.sub(r"<[^>]+>", "", attrs.get("description", "") or "").strip()
    # Fallback to main data
    attrs = data.get("data", {}).get("attributes", {})
    return re.sub(r"<[^>]+>", "", attrs.get("description", "") or "").strip()


TARIFF_CACHE_MAX_AGE_DAYS = 30  # Gov.uk updates tariff rates monthly


def _tariff_is_stale(tariff: dict) -> bool:
    """Return True if the cached tariff entry was fetched more than
    TARIFF_CACHE_MAX_AGE_DAYS ago (so we should refetch from gov.uk)."""
    if not tariff:
        return True
    fetched = tariff.get("fetched_at")
    if not fetched:
        return True  # legacy entry without timestamp
    try:
        from datetime import datetime, timezone, timedelta
        # Handle both with and without timezone
        ts = fetched.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt
        return age > timedelta(days=TARIFF_CACHE_MAX_AGE_DAYS)
    except Exception:
        return True


async def lookup_tariff(commodity_code: str) -> dict:
    """Wrapper: calls _lookup_tariff_raw and stamps fetched_at for cache aging."""
    from datetime import datetime, timezone
    result = await _lookup_tariff_raw(commodity_code)
    if isinstance(result, dict):
        result["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return result


async def _lookup_tariff_raw(commodity_code: str) -> dict:
    """Query UK Trade Tariff API for duty/VAT rates + possible sub-codes.

    EU invoices use 8-digit codes. The UK API requires 10-digit leaf codes.
    An 8-digit code padded to 10 is often a non-leaf (parent) node → 404.
    Strategy:
      1. Try /commodities/{10-digit} — if 200, it's a leaf → extract duty/vat.
      2. If 404, try /subheadings/{10-digit}-80 to get all child leaf codes.
      3. Return description, duty, vat, and subcodes list.
    """
    code = re.sub(r"\D", "", commodity_code)
    if not code:
        return {}
    code10 = code.ljust(10, "0")[:10]
    headers = {"Accept": "application/json"}
    _empty = {"duty": "N/A", "vat": "0%", "description": "", "subcodes": []}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Step 1: try direct commodity lookup
            r = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/commodities/{code10}",
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                duty, vat = _extract_duty_vat(data)
                attrs = data.get("data", {}).get("attributes", {})
                desc = re.sub(r"<[^>]+>", "", attrs.get("description", ""))
                return {
                    "description": desc.strip(),
                    "duty": duty or "N/A",
                    "vat": vat or "0%",
                    "subcodes": [{
                        "code": code10,
                        "description": desc.strip(),
                        "duty": duty or "N/A",
                    }],
                }

            # Step 2: non-leaf → get subheading children
            r2 = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/subheadings/{code10}-80",
                headers=headers,
            )
            if r2.status_code == 200:
                data2 = r2.json()
                # Collect child commodity codes from included[]
                subcodes = []
                included = data2.get("included", [])

                # Build a map of commodity IDs to their measures
                measures_by_commodity: dict[str, str] = {}
                for item in included:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "measure":
                        attrs = item.get("attributes", {})
                        mtype = attrs.get("measure_type_description", "")
                        if "Third country duty" in mtype:
                            duty_expr = attrs.get("duty_expression", {})
                            if isinstance(duty_expr, dict):
                                base = re.sub(r"<[^>]+>", "", duty_expr.get("base", "") or "").strip()
                                if base:
                                    # Link to parent commodity via relationships
                                    sid = item.get("id", "")
                                    measures_by_commodity[sid] = base

                for item in included:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "commodity":
                        continue
                    attrs = item.get("attributes", {})
                    item_code = attrs.get("goods_nomenclature_item_id", "")
                    item_desc = re.sub(r"<[^>]+>", "", attrs.get("description", "") or "").strip()
                    leaf = attrs.get("leaf", False)
                    if item_code and leaf:
                        subcodes.append({
                            "code": item_code,
                            "description": item_desc,
                            "duty": "N/A",  # Will try to fill below
                        })

                # Fetch duty + description for each leaf sub-code (max 8)
                first_vat = "0%"
                for sc in subcodes[:8]:
                    try:
                        rs = await client.get(
                            f"https://www.trade-tariff.service.gov.uk/api/v2/commodities/{sc['code']}",
                            headers=headers,
                        )
                        if rs.status_code == 200:
                            ds = rs.json()
                            d, v = _extract_duty_vat(ds)
                            sc["duty"] = d or "N/A"
                            if not first_vat or first_vat == "0%":
                                first_vat = v or "0%"
                            # Get description from the commodity response
                            cdesc = _extract_commodity_desc(ds, sc["code"])
                            if cdesc:
                                sc["description"] = cdesc
                    except Exception:
                        pass

                attrs2 = data2.get("data", {}).get("attributes", {})
                desc2 = re.sub(r"<[^>]+>", "", attrs2.get("description", "") or "").strip()
                first_duty = subcodes[0]["duty"] if subcodes else "N/A"
                return {
                    "description": desc2,
                    "duty": first_duty,
                    "vat": first_vat,
                    "subcodes": subcodes[:10],
                }

            # Step 3: fallback to heading
            r4 = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/headings/{code[:4]}",
                headers=headers,
            )
            if r4.status_code == 200:
                data4 = r4.json()
                attrs4 = data4.get("data", {}).get("attributes", {})
                desc4 = re.sub(r"<[^>]+>", "", attrs4.get("description", "") or "").strip()
                return {
                    "description": desc4,
                    "duty": "N/A",
                    "vat": "0%",
                    "subcodes": [],
                }

            return _empty
    except Exception:
        return {"duty": "N/A", "vat": "0%", "description": "", "subcodes": []}


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------
PROMPT_EXTRACT = """DATA EXTRACTION — STEP 1

CRITICAL OUTPUT RULE — READ FIRST
Your entire response must be ONLY a TSV table. No thinking, no explanations,
no comments, no lists, no prose, no markdown. The very first character of
your response must be the letter "I" (from "Invoice" in the header row).
The very last character must be the final value of the last row.
If you write ANY text outside the TSV table, the output is unusable.
Do not describe what you are doing. Just output the table.

Goal
Extract all line items from the attached invoice into ONE TSV table,
following the exact column order and rules below.

Hard rules (strict — no guessing)
- Extract only information explicitly present on the invoice.
- Do NOT invent commodity codes, descriptions, quantities,
  weights, values, currencies, or countries.
- If a field is missing, leave the cell BLANK.
- Never infer or assume missing information.

Output format
- TAB-SEPARATED VALUES (TSV) only.
- First row = header row exactly: Invoice\tComm./imp. cod\tDescription of Goods\tOrigin\tCountry\tNumber of Packages\tGross Weight (KG)\tNet Weight (KG)\tValue
- EVERY data row MUST have exactly 9 TAB-separated fields (blank cells allowed,
  but there must still be the correct number of tabs). NEVER shift columns:
  if the invoice has no per-line weight, Gross Weight and Net Weight are BOTH
  BLANK and the line total goes in the Value column — never cascade values
  up through empty columns.
- No explanations, comments, or text outside the table.
- No markdown code fences. No ``` anywhere.

Columns (exactly in this order)
1. Invoice            The invoice/document number (Fattura Nr, Invoice No, Nr, Rechnung-Nr).
                      Look near headers like "FATTURA", "INVOICE", "Fattura Accompagnatoria".
                      It is NOT the client reference (ns/rif), NOT the customer number (cliente),
                      NOT a monetary amount. It is usually a short alphanumeric code
                      (e.g. "SE 2692", "FAT/2026/001", "INV-12345").
                      Use the SAME invoice number for every row from the same document.
2. Comm./imp. cod     HS/commodity code — ONLY the customs tariff code, NOT internal SKUs.
                      Valid examples: "07020010", "04061030", "nomenclatura 07094000", HS codes.
                      Invalid — DO NOT use these: "22.289", "115.201", "CC0455.025", "453.000",
                      article/product/SKU numbers that contain a dot or start with letters.
                      These are internal item codes from the supplier, NOT customs codes.
                      If the invoice line does not show a real customs/HS/nomenclature code,
                      LEAVE THIS CELL BLANK. Do NOT use the article number as a fallback.
                      See the formatting rules below for valid code handling.
3. Description of Goods   Product description exactly as written. Blank if absent.
4. Origin             Country of origin — ISO Alpha-2 code (e.g. IT, ES, CN, JP, TW, CH).
                      IMPORTANT: On many invoices the origin is written as a bare 2-letter
                      code between the description and the unit price (no column header).
                      Examples of lines from a Swiss/Caran d'Ache style invoice:
                        "0005 115.201 50 BRUSH WITH WATER RESERVOIR LARGE JP 2.05 102.50"
                         → Origin = JP
                        "0009 117.103 30 ARTIST PLEXIGLASS PALETTE WHITE 26x13MM CN 3.17 95.10"
                         → Origin = CN
                      A 2-letter capitalized token immediately before the unit price is
                      ALWAYS the origin code. Extract it. If no such code exists and the
                      invoice also has no explicit origin column, leave blank.
5. Country            Full English country name matching the ISO code.
6. Number of Packages     Number of SHIPPING PACKAGES / CARTONS / COLLI for that line.
                          This is the physical package count — look for columns labelled
                          "Colli", "Numero Colli", "Cartons", "Packages", "Pkgs", "Colis",
                          "Kartons", "Pallets", "CT", "Boxes". It is NOT the unit quantity
                          (pieces, bottles, pcs, stuks, pz, units) and NOT the order qty.
                          If the invoice only shows a total NUMERO COLLI at the bottom and
                          no per-line package count, leave this BLANK for every line.
                          Never compute it from unit quantity.
7. Gross Weight (KG)  Take the value from the invoice's GROSS WEIGHT column only
                      ("Peso Lordo", "Gross Weight", "Brutto", "Poids Brut", "G.W.").
                      In KG only. Do NOT calculate from unit weight × quantity.
                      Blank if the line has no gross weight column entry.
                      If the invoice only reports a single TOTAL gross weight at the
                      bottom (e.g. "GROSS WEIGHT : 1'081.790 KGS") and no per-line
                      weights, leave this BLANK for every line — Run C will pick up
                      the total separately.
8. Net Weight (KG)    Take the value from the invoice's NET WEIGHT column only
                      ("Peso Netto", "Net Weight", "Netto", "Poids Net", "N.W.").
                      In KG only. Do NOT calculate from unit weight × quantity.
                      Blank if the line has no net weight column entry.
                      Same rule as Gross Weight: blank if only a total is shown.
9. Value              Line total with currency symbol (€ / $ / £ / CHF). 2 decimals.
                      The Value is the LAST/RIGHTMOST numeric amount on the line —
                      the line total (not unit price, not quantity, not weight).
                      On Swiss/Caran d'Ache style invoices:
                        "0005 115.201 50 BRUSH WITH WATER RESERVOIR JP 2.05 102.50"
                        → Value = CHF 102.50 (the last number, line total)
                        → 2.05 is unit price (ignore)
                        → 50 is quantity (ignore)
                      Value is ALWAYS filled unless the line is a note or a header.
                      Never confuse Value with Gross/Net Weight. If the invoice has no
                      per-line weight column, the weight cells stay BLANK — never put
                      the price there.

CONCRETE EXAMPLES — match these patterns exactly

Example 1 — Italian invoice with per-line weights + commodity codes:
Input line:
  "30  CIMA RAPA  IT KG  1  8,80  ,70  8,10  4,000  32,40
   nomenclatura 07049010"
Expected TSV row (9 columns, tab-separated):
  91021436<TAB>07049010<TAB>CIMA RAPA<TAB>IT<TAB>Italy<TAB>1<TAB>8.80<TAB>8.10<TAB>€32.40

Example 2 — Swiss Caran d'Ache invoice, NO commodity code, NO per-line weight:
Input line:
  "0001 3.289 30 FIXPENCIL MECH.PEN BLACK 3MM ASS. 8.22 246.60"
Expected TSV row (9 columns, tab-separated):
  91021436<TAB><TAB>FIXPENCIL MECH.PEN BLACK 3MM ASS.<TAB><TAB><TAB><TAB><TAB><TAB>CHF 246.60
(Notice: commodity code blank, origin blank, country blank, packages blank,
gross weight blank, net weight blank. The line total CHF 246.60 goes in the
LAST column — Value — never in Gross Weight.)

Example 3 — Swiss line WITH origin:
Input line:
  "0005 115.201 50 BRUSH WITH WATER RESERVOIR LARGE JP 2.05 102.50"
Expected TSV row:
  91021436<TAB><TAB>BRUSH WITH WATER RESERVOIR LARGE<TAB>JP<TAB>Japan<TAB><TAB><TAB><TAB>CHF 102.50

Remember: 9 columns always. Value is ALWAYS the last column and contains the
line total with currency symbol. Weight columns are ONLY for real weight data
from a weight column on the invoice — NEVER for monetary values.

Commodity code formatting rules
Step 1 — Determine supplier country from VAT number prefix or address:
  • VAT prefix IT, ES, FR, DE, NL, BE, PL, PT, … → EU supplier
  • VAT prefix GB → UK supplier
  • All others → Unknown

Step 2 — Apply the correct digit length:
  • EU supplier  → 8 digits. Truncate to 8 if longer, right-pad with zeros if shorter.
  • UK supplier  → 10 digits. Right-pad with zeros if shorter.
  • Unknown      → Copy exactly as printed, no modification.
  Never invent or change the digits themselves — only pad or truncate.

Sorting: by Invoice (ascending) → Commodity code (ascending, numeric).

Grouping: merge rows where Invoice + Commodity code + Description + Origin are identical.
For merged rows SUM: gross weight, net weight, value, and packages (only if
packages were filled per line — otherwise keep blank).
All numeric values: 2 decimals. Values must be numbers (positive or negative).
"""

PROMPT_VERIFY = (
    "This is a second independent extraction. "
    "Read the invoice fresh — do not reference any previous result.\n\n"
    + PROMPT_EXTRACT
)

PROMPT_TOTALS = """INVOICE TOTALS EXTRACTION

Look ONLY at the invoice's SUMMARY / FOOTER / TOTALS section — the part where
the invoice reports its own grand totals. Common labels:
  • Total packages / Numero Colli / Total Colli / Total Cartons / Total Pkgs
  • Pallets / Pallet count / N X PALLETS / BOX PALLETS
  • Total Gross Weight / Peso Lordo Totale / Brutto Totale / Total G.W. / GROSS WEIGHT
  • Total Net Weight / Peso Netto Totale / Netto Totale / Total N.W. / NET WEIGHT
  • Total / Totale / Total Amount / Grand Total / Importo Totale / Invoice Total

Report the values EXACTLY as they appear in that summary — do NOT compute or
sum them yourself from line items.

Special case — pallet arithmetic:
  If the invoice says "4 X BOX PALLETS + 1 X PALLET", that is 4 + 1 = 5 pallets.
  Output total_packages\\t5.
  If it says "3 PALLETS", output total_packages\\t3.
  Treat pallets, boxes, cartons, and colli as interchangeable for the package count.

OUTPUT FORMAT — STRICT
Your entire response must be ONLY these 4 lines, in this exact order,
with a TAB between key and value. No prose, no explanations, no markdown.
If a value is not explicitly shown in the summary, leave it blank after the tab.

total_packages\t<number or blank>
total_gross_kg\t<number in KG or blank>
total_net_kg\t<number in KG or blank>
total_value\t<number with currency symbol or blank>

Rules:
- Numbers only (plus currency symbol on total_value). No units like "KG" or "colli".
- Use a dot as decimal separator (e.g. 1234.56).
- Thousand separators: accept both comma and apostrophe (1,081.79 or 1'081.79).
  Always output with dot as decimal separator: 1081.79.
- If the invoice shows "NUMERO COLLI 705", output: total_packages\t705
- If it shows "GROSS WEIGHT : 1'081.790 KGS", output: total_gross_kg\t1081.79
- Never guess. Blank is better than wrong.
"""

COLUMNS = [
    "Invoice",
    "Comm./imp. cod",
    "Description of Goods",
    "Origin",
    "Country",
    "Number of Packages",
    "Gross Weight (KG)",
    "Net Weight (KG)",
    "Value",
]


def parse_tsv(tsv: str) -> list[dict]:
    lines = [l for l in tsv.strip().splitlines() if l.strip()]
    if not lines:
        return []
    # Find header row
    header_line = 0
    for i, line in enumerate(lines):
        if "Invoice" in line or "Comm" in line:
            header_line = i
            break
    headers = [h.strip() for h in lines[header_line].split("\t")]
    rows = []
    for line in lines[header_line + 1:]:
        parts = line.split("\t")
        # pad/trim to match headers
        while len(parts) < len(headers):
            parts.append("")
        row = {headers[i]: parts[i].strip() for i in range(len(headers))}
        rows.append(row)
    return rows


def normalise_row(row: dict) -> dict:
    """Map any variant header names to canonical COLUMNS."""
    mapping = {
        "invoice": "Invoice",
        "comm./imp. cod": "Comm./imp. cod",
        "comm/imp cod": "Comm./imp. cod",
        "commodity code": "Comm./imp. cod",
        "description of goods": "Description of Goods",
        "description": "Description of Goods",
        "origin": "Origin",
        "country": "Country",
        "number of packages": "Number of Packages",
        "packages": "Number of Packages",
        "gross weight (kg)": "Gross Weight (KG)",
        "gross weight": "Gross Weight (KG)",
        "net weight (kg)": "Net Weight (KG)",
        "net weight": "Net Weight (KG)",
        "value": "Value",
    }
    out = {}
    for k, v in row.items():
        canon = mapping.get(k.lower().strip(), k)
        out[canon] = v
    return out


def _norm_num(s: str) -> str:
    """Normalize a numeric string for comparison.
    Handles EU (1.234,56) and US (1,234.56) formats, strips currency symbols.
    Returns the integer representation (cents-like) for robust comparison."""
    if not s:
        return ""
    cleaned = re.sub(r"[^\d,\.\-]", "", str(s))
    if not cleaned:
        return ""
    # If both . and , present: last one is decimal separator
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Comma only — treat as decimal if 1-2 digits after, else thousands
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return f"{float(cleaned):.2f}"
    except ValueError:
        return ""


def _row_key(r: dict) -> tuple:
    """Stable sort key for a row (commodity code + description)."""
    return (
        re.sub(r"\D", "", r.get("Comm./imp. cod", "") or ""),
        (r.get("Description of Goods", "") or "").strip().lower(),
    )


def is_real_commodity_code(code: str) -> bool:
    """Return True only for strings that look like real HS/commodity codes.
    Real codes: 6-10 contiguous digits with NO dots, NO letters, NO slashes.
    Our extraction pipeline stores them as plain digits ("07094000",
    "04061030"). Anything else is almost certainly an internal SKU
    (22.289, 999.190, CC0455.025, 115.201, PRD-123)."""
    if not code:
        return False
    stripped = code.strip()
    # Must be all digits (possibly with surrounding whitespace we already stripped)
    if not stripped.isdigit():
        return False
    if len(stripped) < 6 or len(stripped) > 10:
        return False
    return True


def rows_match(a: list[dict], b: list[dict]) -> tuple[bool, list[str]]:
    """Check if two extractions match on key numeric/code fields,
    after sorting rows so order differences don't cause false mismatches.
    Returns (match: bool, reasons: list of mismatch descriptions)."""
    reasons: list[str] = []
    if len(a) != len(b):
        reasons.append(f"Row count differs: Run A has {len(a)} rows, Run B has {len(b)} rows")
        return False, reasons
    sa = sorted(a, key=_row_key)
    sb = sorted(b, key=_row_key)
    for i, (ra, rb) in enumerate(zip(sa, sb)):
        desc_a = (ra.get("Description of Goods", "") or "").strip()[:40]
        desc_b = (rb.get("Description of Goods", "") or "").strip()[:40]
        row_label = desc_a or desc_b or f"row {i+1}"
        # Commodity code: compare digits only
        ca = re.sub(r"\D", "", ra.get("Comm./imp. cod", "") or "")
        cb = re.sub(r"\D", "", rb.get("Comm./imp. cod", "") or "")
        if ca and cb and ca != cb:
            reasons.append(f"[{row_label}] Code differs: A={ca} vs B={cb}")
        # Numeric fields: compare normalized values
        for field in ("Value", "Gross Weight (KG)", "Net Weight (KG)"):
            va = _norm_num(ra.get(field, ""))
            vb = _norm_num(rb.get(field, ""))
            if va and vb and va != vb:
                reasons.append(f"[{row_label}] {field} differs: A={va} vs B={vb}")
    return len(reasons) == 0, reasons


def parse_totals(raw: str) -> dict:
    """Parse the Run C totals output into a dict of normalized numbers.
    Keys: total_packages, total_gross_kg, total_net_kg, total_value.
    Also keeps total_value_raw (with currency symbol) for display."""
    out = {
        "total_packages": "",
        "total_gross_kg": "",
        "total_net_kg": "",
        "total_value": "",
        "total_value_raw": "",
    }
    if not raw:
        return out
    for line in raw.strip().splitlines():
        if "\t" not in line:
            continue
        k, v = line.split("\t", 1)
        k = k.strip().lower()
        v = v.strip()
        if k not in out:
            continue
        if k == "total_value":
            out["total_value_raw"] = v
            out["total_value"] = _norm_num(v)
        else:
            out[k] = _norm_num(v)
    return out


def sum_rows_numeric(rows: list[dict], field: str) -> str:
    """Sum a numeric column across rows, return normalized string (2 decimals)."""
    total = 0.0
    any_val = False
    for r in rows:
        n = _norm_num(r.get(field, ""))
        if n:
            try:
                total += float(n)
                any_val = True
            except ValueError:
                pass
    return f"{total:.2f}" if any_val else ""


def compare_totals(rows: list[dict], totals: dict) -> dict:
    """Compare summed rows against invoice totals.
    Returns a dict per field: {reported, computed, match}.
    A field with no reported total is skipped (match=None)."""
    def close(a: str, b: str, tol: float = 0.02) -> bool:
        """Tolerance: 0.02 absolute for small values, 1% relative for large."""
        if not a or not b:
            return False
        try:
            fa, fb = float(a), float(b)
        except ValueError:
            return False
        if abs(fa - fb) <= tol:
            return True
        if max(abs(fa), abs(fb)) > 0:
            return abs(fa - fb) / max(abs(fa), abs(fb)) <= 0.01
        return False

    checks = {}
    mapping = {
        "total_value": "Value",
        "total_gross_kg": "Gross Weight (KG)",
        "total_net_kg": "Net Weight (KG)",
        "total_packages": "Number of Packages",
    }
    for tkey, rfield in mapping.items():
        reported = totals.get(tkey, "")
        computed = sum_rows_numeric(rows, rfield)
        if not reported:
            # Invoice didn't report this total — can't verify, treat as N/A
            checks[tkey] = {"reported": "", "computed": computed, "match": None}
        elif not computed:
            # Invoice reports a total but there's no per-line data to sum.
            # This is common on invoices that only show totals in the footer
            # (e.g. Swiss Caran d'Ache style). Not a mismatch — just unverifiable.
            checks[tkey] = {"reported": reported, "computed": "", "match": None}
        else:
            checks[tkey] = {
                "reported": reported,
                "computed": computed,
                "match": close(reported, computed),
            }
    return checks


def extract_value_number(val_str: str) -> float | None:
    if not val_str:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", val_str)
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract plain text from PDF locally (saves ~80% input tokens vs sending raw PDF)."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages = []
            for page in pdf.pages:
                txt = page.extract_text() or ""
                pages.append(txt)
            return "\n\n--- PAGE BREAK ---\n\n".join(pages)
    except Exception:
        return ""


def extract_pdf_pages(file_bytes: bytes) -> list[str]:
    """Return extracted text per page as a list."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return [(p.extract_text() or "") for p in pdf.pages]
    except Exception:
        return []


def chunk_pages(pages: list[str], max_words_per_chunk: int = 1800) -> list[str]:
    """Group pages into chunks that stay under the rate limit.
    ~1800 words ≈ 2400 tokens, safely under 10K/min single-request budget."""
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for p in pages:
        w = len(p.split())
        if current and current_words + w > max_words_per_chunk:
            chunks.append("\n\n--- PAGE BREAK ---\n\n".join(current))
            current = [p]
            current_words = w
        else:
            current.append(p)
            current_words += w
    if current:
        chunks.append("\n\n--- PAGE BREAK ---\n\n".join(current))
    return chunks


async def run_extraction_text(client: anthropic.AsyncAnthropic, pdf_text: str, prompt: str, model: str | None = None) -> str:
    """Run extraction on pre-extracted PDF text with prompt caching.
    Run A writes cache, Run B reads cache at ~10% cost. Retries on rate limit."""
    model = model or AI_MODEL_PRIMARY
    for attempt in range(5):
        try:
            message = await client.messages.create(
                model=model,
                # 32000 tokens ≈ ~1600 invoice rows. Safe headroom for even the
                # biggest invoices (Caran d'Ache at 490 rows used ~10K tokens).
                max_tokens=32000,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"INVOICE TEXT:\n\n{pdf_text}",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            return message.content[0].text
        except anthropic.RateLimitError:
            if attempt == 4:
                raise
            await asyncio.sleep(30 + attempt * 15)
    return ""


async def run_extraction(client: anthropic.AsyncAnthropic, file_bytes: bytes, mime: str, prompt: str, model: str | None = None) -> str:
    """Send file + prompt to Claude and return raw text response.

    For PDFs: extract text locally first (drastically reduces input tokens).
    For images: send as vision input.
    """
    model = model or AI_MODEL_PRIMARY
    content_blocks: list = []

    if mime == "application/pdf":
        pdf_text = extract_pdf_text(file_bytes)
        if pdf_text.strip():
            # Send extracted text — cheap and fits within rate limits
            content_blocks.append({
                "type": "text",
                "text": f"INVOICE TEXT (extracted from PDF):\n\n{pdf_text}",
                "cache_control": {"type": "ephemeral"},
            })
        else:
            # Fallback: send raw PDF if text extraction failed (scanned PDF)
            b64 = base64.standard_b64encode(file_bytes).decode()
            content_blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64},
                "cache_control": {"type": "ephemeral"},
            })
    elif mime.startswith("image/"):
        b64 = base64.standard_b64encode(file_bytes).decode()
        content_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
            "cache_control": {"type": "ephemeral"},
        })
    else:
        # DOCX / other
        b64 = base64.standard_b64encode(file_bytes).decode()
        content_blocks.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            "cache_control": {"type": "ephemeral"},
        })

    content_blocks.append({"type": "text", "text": prompt})

    message = await client.messages.create(
        model=model,
        max_tokens=32000,
        messages=[{"role": "user", "content": content_blocks}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Excel export — matches reference format exactly
# Colors: header fill #1F3864, alt row #DCE6F1, totals fill #2E75B6
# ---------------------------------------------------------------------------
_FILL_HEADER = PatternFill("solid", fgColor="1F3864")   # dark navy header
_FILL_ALT    = PatternFill("solid", fgColor="DCE6F1")   # light blue alt rows
_FILL_TOTALS = PatternFill("solid", fgColor="2E75B6")   # medium blue totals
_FONT_TITLE  = Font(name="Calibri", bold=True, color="1F3864", size=12)
_FONT_HDR    = Font(name="Calibri", bold=True, color="FFFFFF",  size=10)
_FONT_CELL   = Font(name="Calibri", color="000000", size=10)
_FONT_TOTALS = Font(name="Calibri", bold=True, color="FFFFFF",  size=10)

# col: (width, number_format, h_align)
_COL_CFG = [
    ("Invoice",             12,  "General", "left"),
    ("Comm./imp. cod",      18,  "@",       "left"),
    ("Description of Goods",42,  "General", "left"),
    ("Origin",               8,  "General", "center"),
    ("Country",             12,  "General", "left"),
    ("Number of Packages",   8,  "#,##0.00","right"),
    ("Gross Weight (KG)",   18,  "#,##0.00","right"),
    ("Net Weight (KG)",     16,  "#,##0.00","right"),
    ("Value",               14,  '\u20ac#,##0.00', "right"),
]


def build_excel(rows: list[dict], tariff_data: dict | None, sheet_title: str) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title

    # ── Row 1: merged title ───────────────────────────────────
    ws.merge_cells("A1:I1")
    # Build title from first data row
    inv_num  = rows[0].get("Invoice", "") if rows else ""
    date_str = datetime.now().strftime("%d/%m/%Y")
    title_val = f"Invoice {inv_num} — {date_str}" if inv_num else sheet_title
    c = ws["A1"]
    c.value     = title_val
    c.font      = _FONT_TITLE
    c.fill      = PatternFill("solid", fgColor="FFFFFF")
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22

    # ── Row 2: column headers ─────────────────────────────────
    for col_idx, (col_name, width, fmt, align) in enumerate(_COL_CFG, start=1):
        c = ws.cell(row=2, column=col_idx, value=col_name)
        c.font      = _FONT_HDR
        c.fill      = _FILL_HEADER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 30

    # Freeze at row 3
    ws.freeze_panes = "A3"

    # ── Data rows ─────────────────────────────────────────────
    data_start = 3
    for row_idx, row in enumerate(rows, start=data_start):
        fill = _FILL_ALT if (row_idx % 2 == 0) else None   # even = light blue, odd = white
        for col_idx, (col_name, width, fmt, align) in enumerate(_COL_CFG, start=1):
            c   = ws.cell(row=row_idx, column=col_idx)
            raw = row.get(col_name, "") or ""

            if fill:
                c.fill = fill
            c.font      = _FONT_CELL
            c.alignment = Alignment(horizontal=align, vertical="center")
            c.number_format = fmt

            if col_name in ("Value", "Gross Weight (KG)", "Net Weight (KG)", "Number of Packages"):
                num = extract_value_number(str(raw))
                c.value = num if num is not None else raw
            else:
                c.value = str(raw) if raw else ""

    data_end = data_start + len(rows) - 1

    # ── Totals row ────────────────────────────────────────────
    total_row = data_end + 1
    ws.merge_cells(f"A{total_row}:E{total_row}")
    tc = ws.cell(row=total_row, column=1, value="TOTALS")
    tc.font      = _FONT_TOTALS
    tc.fill      = _FILL_TOTALS
    tc.alignment = Alignment(horizontal="left", vertical="center")

    for col_idx, (col_name, width, fmt, align) in enumerate(_COL_CFG, start=1):
        if col_idx < 6:   # already merged
            ws.cell(row=total_row, column=col_idx).fill = _FILL_TOTALS
            continue
        col_letter = get_column_letter(col_idx)
        c = ws.cell(row=total_row, column=col_idx)
        c.fill      = _FILL_TOTALS
        c.font      = _FONT_TOTALS
        c.alignment = Alignment(horizontal="right", vertical="center")
        c.number_format = fmt
        if col_name in ("Gross Weight (KG)", "Net Weight (KG)", "Value"):
            c.value = f"=ROUND(SUM({col_letter}{data_start}:{col_letter}{data_end}),2)"
        else:
            c.value = f"=SUM({col_letter}{data_start}:{col_letter}{data_end})"

    # ── Column widths ─────────────────────────────────────────
    for col_idx, (col_name, width, fmt, align) in enumerate(_COL_CFG, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # ── Tariff sheet (full export only) ───────────────────────
    if tariff_data:
        from openpyxl.styles import Border, Side
        thick_top = Border(top=Side(style="medium", color="1F3864"))

        ws2 = wb.create_sheet("Tariff Lookup")
        tariff_cols = ["Commodity Code", "Product", "Matched Sub-code", "Duty", "VAT"]
        tariff_widths = [18, 46, 20, 22, 10]
        for ci, h in enumerate(tariff_cols, start=1):
            c = ws2.cell(row=1, column=ci, value=h)
            c.font = _FONT_HDR
            c.fill = _FILL_HEADER
            c.alignment = Alignment(horizontal="center", vertical="center")
        for ci, w in enumerate(tariff_widths, start=1):
            ws2.column_dimensions[get_column_letter(ci)].width = w

        row_idx = 2
        prev_code = None
        for r in rows:
            code = (r.get("Comm./imp. cod") or "").strip()
            desc = (r.get("Description of Goods") or "").strip()
            if not code or not desc:
                continue

            info = tariff_data.get(code, {})
            matched_code = r.get("_matched_code", "") or ""
            matched_duty = r.get("_matched_duty", "") or info.get("duty", "")
            vat_val = info.get("vat", "") or ""

            cells = [
                code,
                desc,
                matched_code or "—",
                matched_duty or "—",
                vat_val or "—",
            ]
            for ci, v in enumerate(cells, start=1):
                c = ws2.cell(row=row_idx, column=ci, value=v)
                c.font = _FONT_CELL
                # Thick top border marks a new commodity-code group
                if prev_code is not None and code != prev_code:
                    c.border = thick_top
            prev_code = code
            row_idx += 1

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Subcode matching — ask Claude which sub-code fits the product
# ---------------------------------------------------------------------------
async def match_subcodes(
    client: anthropic.AsyncAnthropic,
    products: list[dict],
    tariff_data: dict[str, Any],
) -> dict[str, dict]:
    """For each product that has multiple possible sub-codes, ask Claude to
    pick the best match. Returns {code::description -> {matched_code, matched_desc, duty}}.

    Only calls Claude once with all products batched together.
    """
    # Build the prompt with all products that need matching
    lines = []
    for row in products:
        code = row.get("Comm./imp. cod", "").strip()
        desc = row.get("Description of Goods", "").strip()
        if not code or not desc:
            continue
        tariff = tariff_data.get(code, {})
        subcodes = tariff.get("subcodes", [])
        if len(subcodes) < 2:
            continue  # Only 1 or 0 subcodes → no choice to make
        key = f"{code}::{desc}"
        sc_list = " | ".join(
            f"{s['code']} = {s['description']}" for s in subcodes
        )
        lines.append(f"- Product: {desc} (invoice code: {code}) → Options: {sc_list}")

    if not lines:
        return {}

    prompt = f"""COMMODITY SUB-CODE MATCHING

For each product below, pick the ONE sub-code that best matches the product description.
Consider what the product actually is — its form, packaging, and characteristics.

Products to classify:
{chr(10).join(lines)}

OUTPUT FORMAT — STRICT
One line per product, TAB-separated: invoice_code\\tproduct_description\\tmatched_subcode
No explanations, no prose. Just the TSV lines.

Example:
04061030\tMOZZARELLA X3 KG.BUF.DOP\t0406103090
07020010\tCHERRY IL MARCHIO X3\t0702001007
"""
    try:
        msg = await client.messages.create(
            model=AI_MODEL_PRIMARY,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        result: dict[str, dict] = {}
        for line in msg.content[0].text.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                inv_code = parts[0].strip()
                prod_desc = parts[1].strip()
                matched = parts[2].strip()
                key = f"{inv_code}::{prod_desc}"
                # Find the matched subcode info
                tariff = tariff_data.get(inv_code, {})
                for sc in tariff.get("subcodes", []):
                    if sc["code"] == matched:
                        result[key] = {
                            "matched_code": matched,
                            "matched_desc": sc.get("description", ""),
                            "duty": sc.get("duty", "N/A"),
                        }
                        break
                else:
                    # Claude returned a code not in our list — store anyway
                    result[key] = {
                        "matched_code": matched,
                        "matched_desc": "",
                        "duty": "N/A",
                    }
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Background processing pipeline
# ---------------------------------------------------------------------------
async def _process_invoice(job_id: str, company_id: str, file_path: Path, original_name: str, mime: str, upload_storage_path: str = ""):
    def update(progress: int, step: str):
        try:
            db.update_job(job_id, {"progress": progress, "step": step})
        except Exception:
            pass

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        file_bytes = file_path.read_bytes()

        # PDF → local text extraction for cheaper API calls
        if mime == "application/pdf":
            pdf_text = extract_pdf_text(file_bytes)
        else:
            pdf_text = ""

        update(10, "Extracting data (Run A)…")
        if pdf_text:
            raw_a = await run_extraction_text(client, pdf_text, PROMPT_EXTRACT)
        else:
            raw_a = await run_extraction(client, file_bytes, mime, PROMPT_EXTRACT)

        update(40, "Verifying (Run B, cached)…")
        if pdf_text:
            raw_b = await run_extraction_text(client, pdf_text, PROMPT_VERIFY)
        else:
            raw_b = await run_extraction(client, file_bytes, mime, PROMPT_VERIFY)

        rows_a = [normalise_row(r) for r in parse_tsv(raw_a)]
        rows_b = [normalise_row(r) for r in parse_tsv(raw_b)]

        update(55, "Cross-checking extractions…")
        ab_match, ab_reasons = rows_match(rows_a, rows_b)
        final_rows = rows_a

        # Run C — totals from footer (simpler task, uses lighter/cheaper model)
        update(65, "Reading invoice totals (Run C)…")
        try:
            if pdf_text:
                raw_c = await run_extraction_text(client, pdf_text, PROMPT_TOTALS, model=AI_MODEL_LIGHT)
            else:
                raw_c = await run_extraction(client, file_bytes, mime, PROMPT_TOTALS, model=AI_MODEL_LIGHT)
        except Exception:
            raw_c = ""
        totals = parse_totals(raw_c)
        totals_check = compare_totals(final_rows, totals)

        totals_ok = all(c["match"] is not False for c in totals_check.values())
        totals_confirmed = sum(1 for c in totals_check.values() if c["match"] is True)
        if not totals_ok:
            verified = False
        elif totals_confirmed >= 2:
            verified = True
        else:
            verified = ab_match

        status = "verified" if verified else "subcode_needed"

        # Step 4 — Tariff lookup (use product memory cache first)
        update(80, "Looking up tariff codes…")
        memory_entries = db.list_memory(company_id)
        memory_by_key = {
            f"{m.get('code','')}::{m.get('description','')}": m for m in memory_entries
        }
        memory_by_code: dict[str, list[dict]] = {}
        for m in memory_entries:
            memory_by_code.setdefault(m.get("code", ""), []).append(m)

        tariff_data: dict[str, Any] = {}
        seen_codes: set[str] = set()
        for row in final_rows:
            code = row.get("Comm./imp. cod", "").strip()
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            cached = None
            for m in memory_by_code.get(code, []):
                t = m.get("tariff") or {}
                if t.get("subcodes") and not _tariff_is_stale(t):
                    cached = t
                    break
            if cached:
                tariff_data[code] = cached
            else:
                # Either no cache or cache is older than 30 days → refetch
                info = await lookup_tariff(code)
                tariff_data[code] = info

        # Step 4b — Match sub-codes to specific products
        update(85, "Matching sub-codes to products…")
        products_to_match = []
        for row in final_rows:
            code = row.get("Comm./imp. cod", "").strip()
            desc = row.get("Description of Goods", "").strip()
            if not code or not desc:
                continue
            key = f"{code}::{desc}"
            existing = memory_by_key.get(key) or {}
            if existing.get("matched_code"):
                continue  # already matched
            tariff = tariff_data.get(code, {})
            if len(tariff.get("subcodes", [])) >= 2:
                products_to_match.append(row)

        matched_codes: dict[str, dict] = {}
        if products_to_match:
            matched_codes = await match_subcodes(client, products_to_match, tariff_data)

        # Enrich each row with its matched sub-code (from memory if already
        # known, otherwise from this run's match_subcodes). This lets the
        # Excel export and any consumer see the per-product match directly.
        for row in final_rows:
            code = row.get("Comm./imp. cod", "").strip()
            desc = row.get("Description of Goods", "").strip()
            if not code or not desc:
                continue
            key = f"{code}::{desc}"
            existing = memory_by_key.get(key) or {}
            if existing.get("matched_code"):
                row["_matched_code"] = existing["matched_code"]
                row["_matched_desc"] = existing.get("matched_desc", "")
                row["_matched_duty"] = existing.get("matched_duty", "")
            elif matched_codes.get(key):
                m = matched_codes[key]
                row["_matched_code"] = m.get("matched_code", "")
                row["_matched_desc"] = m.get("matched_desc", "")
                row["_matched_duty"] = m.get("duty", "")

        # Persist memory updates to database — BUT ONLY IF the invoice is verified.
        # Unverified / subcode_needed invoices don't touch product memory to avoid
        # learning wrong data. Memory is populated later when the user confirms
        # the invoice via /resolve.
        if verified:
            for row in final_rows:
                code = row.get("Comm./imp. cod", "").strip()
                desc = row.get("Description of Goods", "").strip()
                if not code or not desc:
                    continue
                # Skip rows whose "code" is actually an internal SKU
                if not is_real_commodity_code(code):
                    continue
                key = f"{code}::{desc}"
                existing = memory_by_key.get(key)
                tariff_info = tariff_data.get(code, {})
                match_info = matched_codes.get(key, {})

                if existing:
                    updates: dict = {}
                    old_tariff = existing.get("tariff") or {}
                    if not old_tariff or not old_tariff.get("subcodes"):
                        if tariff_info and tariff_info.get("subcodes"):
                            updates["tariff"] = tariff_info
                    if match_info and not existing.get("matched_code"):
                        updates["matched_code"] = match_info["matched_code"]
                        updates["matched_desc"] = match_info["matched_desc"]
                        updates["matched_duty"] = match_info["duty"]
                    if not existing.get("confirmed"):
                        updates["confirmed"] = True
                    if updates:
                        db.update_memory(existing["id"], company_id, updates)
                else:
                    entry = {
                        "code": code,
                        "description": desc,
                        "confirmed": True,
                        "tariff": tariff_info,
                    }
                    if match_info:
                        entry["matched_code"] = match_info["matched_code"]
                        entry["matched_desc"] = match_info["matched_desc"]
                        entry["matched_duty"] = match_info["duty"]
                    db.upsert_memory(company_id, entry)

        # Step 5 — Generate Excel files + upload to Supabase Storage
        update(95, "Generating Excel files…")
        stem = Path(original_name).stem
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_stem = re.sub(r"[^\w\-]", "_", stem)

        full_bytes = build_excel(final_rows, tariff_data, "Invoice Data")
        raw_bytes  = build_excel(final_rows, None, "Raw Extraction")

        xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        full_storage = f"{company_id}/{safe_stem}_{ts}_full.xlsx"
        raw_storage  = f"{company_id}/{safe_stem}_{ts}_raw.xlsx"
        db.storage_upload(db.BUCKET_EXPORTS, full_storage, full_bytes, xlsx_mime)
        db.storage_upload(db.BUCKET_EXPORTS, raw_storage,  raw_bytes,  xlsx_mime)

        # Keep paths pointing to storage (not local disk) so Render deploys work
        full_path = full_storage
        raw_path  = raw_storage

        # Detect supplier/invoice label from first row
        raw_inv = final_rows[0].get("Invoice", "") if final_rows else ""
        _inv_cleaned = re.sub(r"[^\d]", "", raw_inv)
        _looks_like_amount = bool(
            re.match(r"^[\d.,\s]+$", raw_inv.strip())
            and ("," in raw_inv or "." in raw_inv)
            and len(_inv_cleaned) > 4
        )
        if raw_inv and not _looks_like_amount:
            supplier = raw_inv
        else:
            supplier = re.sub(r"[_\-]+", " ", Path(original_name).stem).strip()

        total_value = 0.0
        currency = "€"
        for row in final_rows:
            v = row.get("Value", "") or ""
            num = extract_value_number(v)
            if num:
                total_value += num
            if "£" in v:
                currency = "£"
            elif "$" in v:
                currency = "$"

        invoice = db.create_invoice(company_id, {
            "supplier":       supplier,
            "filename":       original_name,
            "date":           datetime.now().isoformat(),
            "value":          f"{currency}{total_value:,.2f}",
            "status":         status,
            "rows":           final_rows,
            "tariff_data":    tariff_data,
            "totals":         totals,
            "totals_check":   totals_check,
            "ab_match":       ab_match,
            "ab_reasons":     ab_reasons,
            "full_xlsx_path": str(full_path),
            "raw_xlsx_path":  str(raw_path),
            "upload_path":    upload_storage_path,
        })

        db.update_job(job_id, {
            "status":     "done",
            "invoice_id": invoice["id"],
            "progress":   100,
            "step":       "Complete",
        })

    except Exception as exc:
        try:
            db.update_job(job_id, {
                "status":   "failed",
                "step":     f"Error: {exc}",
                "progress": 0,
                "error":    str(exc),
            })
        except Exception:
            pass
        raise
    finally:
        # Clean up the temp local file (original is safe in Supabase Storage)
        try:
            if file_path.exists():
                file_path.unlink()
        except Exception:
            pass



# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Invoice Sorter")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    session_cookie="is_session",
    max_age=60 * 60 * 12,  # 12 hours
    https_only=False,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

MIME_MAP = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    login_html = BASE_DIR / "static" / "login.html"
    return HTMLResponse(content=login_html.read_text(encoding="utf-8"))


@app.post("/api/login")
async def api_login(request: Request, body: dict = {}):
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    company_name = (body.get("company") or "").strip()

    # If company is provided, look up within that company; else search any
    company = None
    if company_name:
        company = db.get_company_by_name(company_name)
        if not company:
            raise HTTPException(status_code=401, detail="Company not found")
        user = db.get_user(username, company["id"])
    else:
        # No company specified — default company only (for backward compat)
        user = db.get_user(username, db.DEFAULT_COMPANY_ID)

    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    request.session["user_id"]    = user["id"]
    request.session["username"]   = user["username"]
    request.session["company_id"] = user["company_id"]
    request.session["role"]       = user.get("role", "user")
    return {
        "ok": True,
        "user": user["username"],
        "role": user.get("role", "user"),
        "company_id": user["company_id"],
    }


@app.post("/api/admin/companies")
async def api_create_company(body: dict = {}, _: dict = Depends(require_super_admin)):
    """Super-admin only: provision a new customer company + first admin user."""
    company_name = (body.get("company") or "").strip()
    username     = (body.get("username") or "").strip().lower()
    password     = body.get("password") or ""
    if not company_name or not username or not password:
        raise HTTPException(400, "Company, username and password are all required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if db.get_company_by_name(company_name):
        raise HTTPException(409, "Company already exists")
    company = db.create_company(company_name)
    db.create_user(company["id"], username, _pwd_ctx.hash(password), "admin")
    return {"ok": True, "company": company}


@app.get("/api/admin/companies")
async def api_list_all_companies(_: dict = Depends(require_super_admin)):
    """Super-admin: list every company with its users."""
    companies = db.list_companies()
    result = []
    for c in companies:
        users = db.list_users(c["id"])
        result.append({**c, "users": users, "user_count": len(users)})
    return result


@app.delete("/api/admin/companies/{company_id}")
async def api_delete_company(company_id: str, _: dict = Depends(require_super_admin)):
    """Super-admin: delete a company (cascades to users, invoices, memory, jobs)."""
    if company_id == db.DEFAULT_COMPANY_ID:
        raise HTTPException(400, "Cannot delete the default company")
    # Find super-admin's own company to prevent self-deletion
    db.sb.table("companies").delete().eq("id", company_id).execute()
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Enrich with company name
    user = db.get_user_by_id(user_id)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="User no longer exists")
    company = next(
        (c for c in db.list_companies() if c["id"] == user["company_id"]),
        None,
    )
    return {
        "user":       user["username"],
        "role":       user.get("role", "user"),
        "company_id": user["company_id"],
        "company":    company["name"] if company else "",
    }


@app.get("/api/users")
def api_list_users(ctx: dict = Depends(require_admin)):
    return db.list_users(ctx["company_id"])


@app.post("/api/users")
async def api_add_user(body: dict = {}, ctx: dict = Depends(require_admin)):
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    role = body.get("role", "user")
    if not username or not password:
        raise HTTPException(400, "Username and password required")
    if db.get_user(username, ctx["company_id"]):
        raise HTTPException(409, "User already exists in this company")
    db.create_user(ctx["company_id"], username, _pwd_ctx.hash(password), role)
    return {"ok": True}


@app.delete("/api/users/{username}")
async def api_delete_user(username: str, ctx: dict = Depends(require_admin)):
    if username == ctx["username"]:
        raise HTTPException(400, "Cannot delete your own account")
    target = db.get_user(username, ctx["company_id"])
    if not target:
        raise HTTPException(404, "User not found")
    db.delete_user(target["id"])
    return {"ok": True}


@app.put("/api/users/{username}/password")
async def api_change_password(username: str, body: dict = {}, ctx: dict = Depends(require_auth)):
    # Admins can change anyone in their company; users can only change their own
    if username != ctx["username"] and ctx["role"] != "admin":
        raise HTTPException(403, "Forbidden")
    target = db.get_user(username, ctx["company_id"])
    if not target:
        raise HTTPException(404, "User not found")
    new_pw = body.get("password") or ""
    if not new_pw:
        raise HTTPException(400, "Password required")
    db.update_user_password(target["id"], _pwd_ctx.hash(new_pw))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Tariff search endpoint
# ---------------------------------------------------------------------------
async def _tariff_code_lookup(code: str) -> list[dict]:
    """Look up a numeric tariff code directly. Handles 4-10 digit codes:
    - 10 digits  → commodity leaf: return with duty/vat + description
    - 8/9 digits → pad to 10 and try commodity; if non-leaf, list all children
    - 6/7 digits → subheading: list children
    - 4/5 digits → heading: list commodities under that heading
    - 2/3 digits → chapter: list headings under that chapter
    """
    headers = {"Accept": "application/json"}
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=15) as client:
        # Try exact commodity first (pad to 10 digits)
        code10 = code.ljust(10, "0")[:10]

        # 1) Direct commodity (leaf) — only if the code was specific enough
        # (8-10 digits). For shorter codes we go straight to listing children.
        if len(code) >= 8:
            r = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/commodities/{code10}",
                headers=headers,
            )
            if r.status_code == 200:
                data = r.json()
                attrs = data.get("data", {}).get("attributes", {})
                # Only treat as leaf result if it's actually declarable
                if attrs.get("declarable"):
                    duty, vat = _extract_duty_vat(data)
                    desc = re.sub(r"<[^>]+>", "", attrs.get("description", "") or "").strip()
                    results.append({
                        "code": code10,
                        "description": desc,
                        "declarable": True,
                        "kind": "commodity",
                        "duty": duty or "—",
                        "vat":  vat  or "0%",
                    })

        # 2) Non-leaf code → try subheading for children
        if not results and len(code) >= 6:
            r2 = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/subheadings/{code10}-80",
                headers=headers,
            )
            if r2.status_code == 200:
                data = r2.json()
                for item in data.get("included", []):
                    if item.get("type") != "commodity":
                        continue
                    a = item.get("attributes", {})
                    cd = a.get("goods_nomenclature_item_id", "")
                    desc = re.sub(r"<[^>]+>", "", a.get("description", "") or "").strip()
                    if cd and desc:
                        results.append({
                            "code": cd,
                            "description": desc,
                            "declarable": bool(a.get("leaf")),
                            "kind": "commodity",
                            "duty": "—",
                            "vat":  "0%",
                        })

        # 3) Heading level (4-digit)
        if not results and len(code) >= 4:
            heading_code = code[:4]
            r3 = await client.get(
                f"https://www.trade-tariff.service.gov.uk/api/v2/headings/{heading_code}",
                headers=headers,
            )
            if r3.status_code == 200:
                data = r3.json()
                for item in data.get("included", []):
                    if item.get("type") != "commodity":
                        continue
                    a = item.get("attributes", {})
                    cd = a.get("goods_nomenclature_item_id", "")
                    desc = re.sub(r"<[^>]+>", "", a.get("description", "") or "").strip()
                    if cd and desc:
                        results.append({
                            "code": cd,
                            "description": desc,
                            "declarable": bool(a.get("leaf")),
                            "kind": "commodity",
                            "duty": "—",
                            "vat":  "0%",
                        })

        # 4) Enrich top 8 declarable commodities with real duty rates
        async def enrich(item):
            if not item.get("declarable"):
                return item
            try:
                info = await lookup_tariff(item["code"])
                if info:
                    item["duty"] = info.get("duty") or "—"
                    item["vat"]  = info.get("vat")  or "0%"
            except Exception:
                pass
            return item

        import asyncio as _asyncio
        top = results[:8]
        enriched = await _asyncio.gather(*(enrich(it) for it in top), return_exceptions=True)
        for i, e in enumerate(enriched):
            if isinstance(e, dict):
                results[i] = e

    return results[:15]


@app.get("/tariff/search")
async def tariff_search(q: str = "", ctx: dict = Depends(require_auth)):
    query = q.strip()
    if not query:
        return []

    # If the query is numeric (e.g. "0406", "04061030", "0406103090"), do a
    # direct lookup instead of fuzzy text search. Users expect to find the
    # exact code they typed, not a text-based match.
    digits_only = re.sub(r"[\s.\-]", "", query)
    if digits_only.isdigit() and 2 <= len(digits_only) <= 10:
        return await _tariff_code_lookup(digits_only)

    url = f"https://www.trade-tariff.service.gov.uk/api/v2/search?q={query}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code != 200:
                return []
            data = r.json()

            # New API structure (2024+): data.attributes.goods_nomenclature_match.commodities
            attrs = data.get("data", {}).get("attributes", {}) or {}
            match = attrs.get("goods_nomenclature_match", {}) or {}
            commodities = match.get("commodities", []) or []
            # Also include heading-level results (broader matches)
            headings = match.get("headings", []) or []

            def collect(items, kind):
                out = []
                for item in items:
                    src = item.get("_source") or item.get("attributes") or {}
                    code = src.get("goods_nomenclature_item_id") or ""
                    desc = src.get("description") or src.get("formatted_description") or ""
                    desc = re.sub(r"<[^>]+>", "", str(desc)).strip()
                    if code and desc and len(code) >= 4:
                        out.append({
                            "code": code,
                            "description": desc,
                            "declarable": bool(src.get("declarable")),
                            "kind": kind,
                            "duty": "—",
                            "vat": "0-20%",
                        })
                return out

            results = collect(commodities, "commodity") + collect(headings, "heading")

            # Fetch actual duty/vat for the top 5 declarable commodities (parallel)
            async def enrich(item):
                if item["kind"] != "commodity" or not item.get("declarable"):
                    return item
                try:
                    info = await lookup_tariff(item["code"])
                    if info:
                        item["duty"] = info.get("duty") or "—"
                        item["vat"] = info.get("vat") or "0%"
                except Exception:
                    pass
                return item

            import asyncio as _asyncio
            top = results[:10]
            enriched = await _asyncio.gather(*(enrich(it) for it in top))
            # Replace the first 10 with enriched versions
            results[:10] = enriched
            return results[:15]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Memory refresh-tariff endpoint
# ---------------------------------------------------------------------------
@app.post("/memory/refresh-tariff")
async def refresh_memory_tariff(
    ctx: dict = Depends(require_auth),
    only_stale: bool = False,
):
    """Refresh tariff data from gov.uk for all memory entries.
    If only_stale=True, only refetch entries older than 30 days or missing data."""
    memory = db.list_memory(ctx["company_id"])
    updated = 0
    for entry in memory:
        tariff = entry.get("tariff") or {}
        if only_stale:
            needs_refresh = (
                not tariff
                or not tariff.get("subcodes")
                or tariff.get("duty") == "N/A"
                or _tariff_is_stale(tariff)
            )
        else:
            # Full refresh mode — refetch everything with real data
            needs_refresh = True
        if needs_refresh:
            code = entry.get("code", "")
            if code:
                info = await lookup_tariff(code)
                if info and (info.get("subcodes") or info.get("duty") != "N/A"):
                    db.update_memory(entry["id"], ctx["company_id"], {"tariff": info})
                    updated += 1
    return {"updated": updated}


@app.post("/memory/refresh-stale")
async def refresh_stale_tariff(ctx: dict = Depends(require_auth)):
    """Refetch tariff data for entries whose cache is older than 30 days.
    Runs much faster than a full refresh when most entries are still fresh."""
    memory = db.list_memory(ctx["company_id"])
    updated = 0
    for entry in memory:
        tariff = entry.get("tariff") or {}
        if _tariff_is_stale(tariff):
            code = entry.get("code", "")
            if code:
                info = await lookup_tariff(code)
                if info:
                    db.update_memory(entry["id"], ctx["company_id"], {"tariff": info})
                    updated += 1
    return {"updated": updated, "total": len(memory)}


@app.post("/upload")
async def upload_invoice(file: UploadFile = File(...), ctx: dict = Depends(require_auth)):
    ext = Path(file.filename).suffix.lower()
    mime = MIME_MAP.get(ext)
    if not mime:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    safe_name = re.sub(r"[^\w\-.]", "_", file.filename)
    content = await file.read()

    # Create the job first so we can use its id as the storage prefix.
    # This way we can always find the original upload by job id — no extra
    # DB column required even for failed jobs that never became an invoice.
    job = db.create_job(ctx["company_id"], {
        "filename": file.filename,
        "status":   "queued",
        "progress": 0,
        "step":     "Waiting in queue…",
    })
    storage_path = f"{ctx['company_id']}/{job['id']}_{safe_name}"
    db.storage_upload(db.BUCKET_UPLOADS, storage_path, content, mime)

    # Local temp copy for the queue worker (fast PDF reads)
    tmp_local = UPLOADS_DIR / f"tmp_{job['id']}_{safe_name}"
    tmp_local.write_bytes(content)

    _enqueue_job(job["id"], ctx["company_id"], tmp_local, file.filename, mime, storage_path)
    return {"job_id": job["id"]}


@app.get("/jobs")
def list_jobs(ctx: dict = Depends(require_auth)):
    raw = db.list_jobs(ctx["company_id"])
    queued = [j for j in raw if j["status"] == "queued"]
    queued_ids = [j["id"] for j in queued]
    result = []
    for j in raw:
        entry = {**j}
        if j["status"] == "queued" and j["id"] in queued_ids:
            pos = queued_ids.index(j["id"]) + 1
            entry["queue_position"] = pos
            entry["queue_total"] = len(queued_ids)
            entry["step"] = f"In queue ({pos}/{len(queued_ids)})…"
        result.append(entry)
    return result


@app.get("/stats")
def get_stats(ctx: dict = Depends(require_auth)):
    cid = ctx["company_id"]
    all_invoices = db.list_invoices(cid)
    total = len(all_invoices)
    verified = sum(1 for v in all_invoices if v["status"] == "verified")
    rate = round(verified / total * 100) if total else 0
    memory = db.list_memory(cid)
    pending = sum(1 for m in memory if not m.get("confirmed", False))
    return {
        "processed_today":   db.count_jobs_today(cid),
        "verification_rate": rate,
        "memory_count":      len(memory),
        "memory_pending":    pending,
    }


@app.get("/invoices")
def list_invoices(ctx: dict = Depends(require_auth)):
    invoices_list = db.list_invoices(ctx["company_id"])
    return [{
        "id":       inv["id"],
        "supplier": inv.get("supplier") or "",
        "filename": inv.get("filename") or "",
        "date":     inv.get("date") or inv.get("created_at"),
        "value":    inv.get("value") or "",
        "status":   inv.get("status") or "",
    } for inv in invoices_list]


@app.get("/invoices/{invoice_id}/debug")
def invoice_debug(invoice_id: str, ctx: dict = Depends(require_auth)):
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")
    rows = inv.get("rows") or []
    return {
        "ab_match":     inv.get("ab_match"),
        "ab_reasons":   inv.get("ab_reasons") or [],
        "rows_a_count": len(rows),
        "rows_b_count": len(rows),
        "totals":       inv.get("totals"),
        "totals_check": inv.get("totals_check"),
        "row_count":    len(rows),
        "rows_preview": rows[:3],
    }


def _stream_storage_file(storage_path: str, suggested_name: str):
    """Download a file from Supabase Storage and stream it to the client."""
    from fastapi.responses import Response
    try:
        data = db.storage_download(db.BUCKET_EXPORTS, storage_path)
    except Exception:
        raise HTTPException(404, "Excel file not found in storage")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{suggested_name}"'},
    )


@app.get("/invoices/{invoice_id}/export/full")
def export_full(invoice_id: str, ctx: dict = Depends(require_auth)):
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")
    storage_path = inv.get("full_xlsx_path") or ""
    if not storage_path:
        raise HTTPException(404, "Excel file not found")
    return _stream_storage_file(storage_path, Path(storage_path).name)


@app.get("/invoices/{invoice_id}/export/raw")
def export_raw(invoice_id: str, ctx: dict = Depends(require_auth)):
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")
    storage_path = inv.get("raw_xlsx_path") or ""
    if not storage_path:
        raise HTTPException(404, "Excel file not found")
    return _stream_storage_file(storage_path, Path(storage_path).name)


@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, ctx: dict = Depends(require_auth)):
    """Retry a failed job by re-downloading its upload from Supabase Storage.
    Used when a job failed before producing an invoice (e.g. out-of-credit)."""
    job = db.get_job(job_id)
    if not job or job.get("company_id") != ctx["company_id"]:
        raise HTTPException(404, "Job not found")
    if job.get("status") not in ("failed", "done"):
        raise HTTPException(400, "Only failed or done jobs can be retried")

    original_file = job.get("filename") or ""
    safe_name = re.sub(r"[^\w\-.]", "_", original_file)
    # Storage path convention used at upload: {company_id}/{job_id}_{safe_name}
    upload_path = f"{ctx['company_id']}/{job_id}_{safe_name}"

    try:
        data = db.storage_download(db.BUCKET_UPLOADS, upload_path)
    except Exception:
        raise HTTPException(404, "Original upload not found in storage")

    ext = Path(original_file).suffix.lower()
    mime = MIME_MAP.get(ext, "application/pdf")

    new_job = db.create_job(ctx["company_id"], {
        "filename": original_file,
        "status":   "queued",
        "progress": 0,
        "step":     "Waiting in queue…",
    })
    # Copy the storage object to the new job's path so future retries also work
    new_storage_path = f"{ctx['company_id']}/{new_job['id']}_{safe_name}"
    db.storage_upload(db.BUCKET_UPLOADS, new_storage_path, data, mime)

    tmp_local = UPLOADS_DIR / f"retry_{new_job['id']}_{safe_name}"
    tmp_local.write_bytes(data)

    _enqueue_job(new_job["id"], ctx["company_id"], tmp_local, original_file, mime, new_storage_path)

    # Remove the old failed job from view
    try:
        db.sb.table("jobs").delete().eq("id", job_id).execute()
    except Exception:
        pass
    return {"job_id": new_job["id"]}


@app.post("/invoices/{invoice_id}/retry")
async def retry_invoice(invoice_id: str, ctx: dict = Depends(require_auth)):
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")

    original_file = inv.get("filename") or ""
    upload_path = inv.get("upload_path") or ""
    if not upload_path:
        raise HTTPException(404, "Original upload not found in storage")

    # Download from Supabase Storage to a local temp file
    try:
        data = db.storage_download(db.BUCKET_UPLOADS, upload_path)
    except Exception:
        raise HTTPException(404, "Original upload could not be downloaded")

    ext = Path(original_file).suffix.lower()
    mime = MIME_MAP.get(ext, "application/pdf")
    tmp_local = UPLOADS_DIR / f"retry_{uuid.uuid4()}_{re.sub(r'[^\\w\\-.]', '_', original_file)}"
    tmp_local.write_bytes(data)

    # Create new job + delete old invoice record
    job = db.create_job(ctx["company_id"], {
        "filename": original_file,
        "status":   "queued",
        "progress": 0,
        "step":     "Waiting in queue…",
    })
    db.delete_invoice(invoice_id, ctx["company_id"])
    _enqueue_job(job["id"], ctx["company_id"], tmp_local, original_file, mime, upload_path)
    return {"job_id": job["id"]}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, ctx: dict = Depends(require_auth)):
    """Delete a job (used by Dismiss on failed jobs). Also cleans up the
    stored PDF upload if it exists."""
    job = db.get_job(job_id)
    if not job or job.get("company_id") != ctx["company_id"]:
        raise HTTPException(404, "Job not found")
    # Best-effort cleanup of the stored upload
    filename = job.get("filename") or ""
    safe_name = re.sub(r"[^\w\-.]", "_", filename)
    upload_path = f"{ctx['company_id']}/{job_id}_{safe_name}"
    try:
        db.storage_delete(db.BUCKET_UPLOADS, upload_path)
    except Exception:
        pass
    # Delete the job record
    db.sb.table("jobs").delete().eq("id", job_id).eq("company_id", ctx["company_id"]).execute()
    return {"ok": True}


@app.delete("/invoices/{invoice_id}")
async def delete_invoice_endpoint(invoice_id: str, ctx: dict = Depends(require_auth)):
    """Permanently delete an invoice and its stored Excel/PDF files."""
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")
    # Best-effort cleanup of storage objects
    for path_field, bucket in (
        ("full_xlsx_path", db.BUCKET_EXPORTS),
        ("raw_xlsx_path",  db.BUCKET_EXPORTS),
        ("upload_path",    db.BUCKET_UPLOADS),
    ):
        p = inv.get(path_field)
        if p:
            try:
                db.storage_delete(bucket, p)
            except Exception:
                pass
    db.delete_invoice(invoice_id, ctx["company_id"])
    return {"ok": True}


@app.post("/invoices/{invoice_id}/resolve")
async def resolve_invoice(invoice_id: str, body: dict = {}, ctx: dict = Depends(require_auth)):
    """Mark a subcode_needed invoice as verified after manual review.
    This also adds the invoice's products to product memory (they were
    held back during processing because the invoice wasn't verified)."""
    inv = db.get_invoice(invoice_id, ctx["company_id"])
    if not inv:
        raise HTTPException(404, "Invoice not found")
    subcode = body.get("subcode", "")
    tariff_data = inv.get("tariff_data") or {}

    # Add every row to product memory (now that the human confirmed it)
    for row in (inv.get("rows") or []):
        code = (row.get("Comm./imp. cod") or "").strip()
        desc = (row.get("Description of Goods") or "").strip()
        if not code or not desc:
            continue
        if not is_real_commodity_code(code):
            continue  # Skip internal SKUs
        existing = db.get_memory_entry(ctx["company_id"], code, desc)
        tariff_info = tariff_data.get(code) or {}
        if existing:
            updates = {"confirmed": True}
            if subcode:
                updates["matched_code"] = subcode
            old_tariff = existing.get("tariff") or {}
            if not old_tariff.get("subcodes") and tariff_info.get("subcodes"):
                updates["tariff"] = tariff_info
            db.update_memory(existing["id"], ctx["company_id"], updates)
        else:
            entry = {
                "code": code,
                "description": desc,
                "confirmed": True,
                "tariff": tariff_info,
            }
            if subcode:
                entry["matched_code"] = subcode
            db.upsert_memory(ctx["company_id"], entry)

    db.update_invoice(invoice_id, ctx["company_id"], {"status": "verified"})
    return {"ok": True}


@app.get("/memory")
def list_memory(ctx: dict = Depends(require_auth)):
    entries = db.list_memory(ctx["company_id"])
    # Expose a stable "key" field for backward compat with frontend
    for e in entries:
        e["key"] = f"{e.get('code','')}::{e.get('description','')}"
    return entries


@app.post("/memory/{entry_id}/confirm")
def confirm_memory(entry_id: str, body: dict = {}, ctx: dict = Depends(require_auth)):
    updates: dict = {"confirmed": True}
    subcode = body.get("subcode", "")
    if subcode:
        updates["matched_code"] = subcode
    db.update_memory(entry_id, ctx["company_id"], updates)
    return {"ok": True}


@app.delete("/memory/{entry_id}")
def delete_memory_entry(entry_id: str, ctx: dict = Depends(require_auth)):
    (db.sb.table("product_memory")
     .delete()
     .eq("id", entry_id)
     .eq("company_id", ctx["company_id"])
     .execute())
    return {"ok": True}


@app.post("/memory/cleanup-invalid")
def cleanup_invalid_memory(ctx: dict = Depends(require_auth)):
    """Remove memory entries whose 'code' is not a real commodity code
    (SKUs, short strings, alphanumeric article numbers)."""
    entries = db.list_memory(ctx["company_id"])
    removed = 0
    for e in entries:
        code = (e.get("code") or "").strip()
        if not is_real_commodity_code(code):
            (db.sb.table("product_memory")
             .delete()
             .eq("id", e["id"])
             .eq("company_id", ctx["company_id"])
             .execute())
            removed += 1
    return {"removed": removed}


# ---------------------------------------------------------------------------
# Static files (must be last)
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
