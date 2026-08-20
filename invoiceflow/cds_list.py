"""Turn a MultiFreight CDS "Items" export into the commodity-code list.

The V-lookup list (`commodity_codes`, formerly the per-client
`client_products`) used to be typed by hand or loaded from a spreadsheet.
A CDS Items export is the same information straight from the source: every
goods line MultiFreight has declared, carrying the commodity code as it was
actually accepted at the border.

This module is pure parsing — no DB, no network — so it can be tested and
re-run over any future export. `scripts/load_commodity_list.py` does the
upserting.

Shape of the export (60 columns, one row per goods line, CP1252):

    Acceptance Date, Imp/Exp, …, Commodity Code, Commodity Description,
    Origin Country, …, Preference, Procedure, …, Documents (…), …

Two columns carry the list:

  * "Commodity Code" — "20059980/98": the 8-digit general code, a slash,
    then the 2-digit TARIC suffix. Concatenated that is the `full_code`
    the export writes; the 8 digits alone are the VLOOKUP `general_code`.
  * "Commodity Description" — what was declared for that line.

The client a line belongs to is NOT in the export (every Exporter/Seller/
Buyer column is empty). The one identifier present is the exporter's REX
in the U116 proof-of-origin document — the same identifier the app already
matches clients on (`db.find_client_by_identity`). The list itself is now
company-wide (shared by all clients), so the REX only feeds the clients
registry; lines with no U116 (a non-EU origin claims no preference) are
grouped under REX "" and load like any other.

A second, thinner source is supported alongside the export: a plain
commodity-code list (spreadsheet or CSV, one row per code — see
`read_code_list`). `add_entries` merges such a list into the derived one,
adding only the codes it does not already hold, so re-running it or
importing an overlapping sheet never writes a code twice.

Run: python -m pytest tests_cds_list.py -q
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict

# The MultiFreight export is Windows-encoded (accented supplier names in
# the description column decode as invalid UTF-8).
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")

_CODE_COL = "Commodity Code"
_DESC_COL = "Commodity Description"
_DATE_COL = "Acceptance Date"
_ORIGIN_COL = "Origin Country"
_PREF_COL = "Preference"
_PROC_COL = "Procedure"
_DOCS_COL = "Documents (Code/ID/Part/Status/Reason/Licence(s)/Date/Qty/Unit)"

# U116 = statement on origin; its ID is the exporter's REX (ITREXIT06167560157).
_REX_RE = re.compile(r"\bU116/([A-Za-z0-9][A-Za-z0-9 .-]*?)/")

# A serial number identifies ONE machine and an MRN ONE declaration —
# never a product line. Carrying either into the list would stamp a past
# shipment's reference onto a future declaration, so everything from the
# keyword onwards is cut from the canonical description.
_SERIAL_RE = re.compile(
    r"\s*\b(?:SERIAL|CHASSIS|ENGINE)\s*(?:NUMBERS?|NOS?\.?|NR\.?|N°)?\s*[:.]?\s*"
    r"[A-Z0-9].*$",
    re.IGNORECASE,
)
_MRN_RE = re.compile(r"\s*\bMRN\s*[:.]?\s*[0-9A-Z][0-9A-Z.-]*\s*$", re.IGNORECASE)


def read_export(source) -> list[dict]:
    """Read a CDS Items export (path, bytes, or open text file) to dict rows."""
    if isinstance(source, (bytes, bytearray)):
        text = _decode(bytes(source))
    elif hasattr(source, "read"):
        data = source.read()
        text = _decode(data) if isinstance(data, (bytes, bytearray)) else data
    else:
        with open(source, "rb") as fh:
            text = _decode(fh.read())
    return list(csv.DictReader(io.StringIO(text, newline="")))


def _decode(raw: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def split_code(raw: str) -> tuple[str, str]:
    """'20059980/98' -> ('20059980', '98'). ('', '') when not a usable code.

    Only a full 8-digit general code is accepted. A 6-digit stub
    ('852910/00' appears three times in the 2026 export) is an incomplete
    declaration line, not a list entry — the caller reports it as skipped
    rather than padding it into a code that was never declared. A 7-digit
    general code is the familiar dropped leading zero (chapters 01-09) and
    IS repaired, matching `_norm_general_code` in main.py.
    """
    left, _, right = str(raw or "").strip().partition("/")
    general = re.sub(r"\D", "", left)
    taric = re.sub(r"\D", "", right)
    if len(general) == 7:
        general = "0" + general
    if len(general) != 8:
        return "", ""
    return general, taric.zfill(2) if taric else "00"


def clean_description(raw: str) -> str:
    """Collapse whitespace, then drop per-shipment serial/MRN references."""
    desc = re.sub(r"\s+", " ", str(raw or "")).strip()
    trimmed = _MRN_RE.sub("", _SERIAL_RE.sub("", desc)).strip(" -,")
    # Only accept the trim if something recognisable is left; a description
    # that is nothing BUT a serial stays as-is rather than becoming empty.
    return trimmed or desc


def rex_of(row: dict) -> str:
    """Exporter REX from the line's U116 document, or '' when absent."""
    m = _REX_RE.search(row.get(_DOCS_COL) or "")
    return m.group(1).strip().upper() if m else ""


def _sort_date(raw: str) -> str:
    """'06/01/2026' -> '2026-01-06' (sortable); '' when unparseable."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", str(raw or ""))
    return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}" if m else ""


def choose_description(candidates: Counter) -> str:
    """The canonical description for one code.

    The export holds every wording ever declared for a code ("OLIVES",
    "GREEN OLIVES STUFFED WITH GARLIC AND PEPPER", "ITALIAN OLIVES"). The
    one that was declared most often is the list description; ties break to
    the shortest, then alphabetically, so a re-run of the same export always
    produces the same list.

    Wordings that differ only in casing ("Meat" / "MEAT") are one wording:
    they are counted together, and the winning group is written in its own
    most-used casing.
    """
    if not candidates:
        return ""
    groups: dict[str, Counter] = defaultdict(Counter)
    for desc, n in candidates.items():
        groups[desc.casefold()][desc] += n
    best = min(groups.items(),
               key=lambda kv: (-sum(kv[1].values()), len(kv[0]), kv[0]))[1]
    return min(best.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def build_list(rows: list[dict]) -> tuple[list[dict], dict]:
    """Fold export rows into list entries, one per (REX, full code).

    Returns (entries, stats). Each entry is ready for
    `db.upsert_commodity_code` (after `merge_by_code` folds the REX groups):

        {rex, general_code, full_code, taric_code, description,
         origin, preference, procedure, lines, last_used}

    `lines` and `last_used` are provenance for the operator's review — how
    many declared goods lines back this code and when it was last used —
    not columns of `commodity_codes`.
    """
    descs: dict[tuple[str, str], Counter] = defaultdict(Counter)
    meta: dict[tuple[str, str], dict] = {}
    stats = {"rows": 0, "used": 0, "skipped_no_code": 0}

    for row in rows:
        stats["rows"] += 1
        general, taric = split_code(row.get(_CODE_COL))
        if not general:
            stats["skipped_no_code"] += 1
            continue
        desc = clean_description(row.get(_DESC_COL))
        if not desc:
            stats["skipped_no_code"] += 1
            continue
        key = (rex_of(row), general + taric)
        descs[key][desc] += 1
        stats["used"] += 1
        seen = _sort_date(row.get(_DATE_COL))
        m = meta.setdefault(key, {"lines": 0, "last_used": "", "origin": "",
                                  "preference": "", "procedure": ""})
        m["lines"] += 1
        if seen >= m["last_used"]:
            # Latest line wins for the situational fields — origin and
            # preference vary per shipment, so they are provenance, not rules.
            m["last_used"] = seen
            m["origin"] = (row.get(_ORIGIN_COL) or "").strip().upper()
            m["preference"] = (row.get(_PREF_COL) or "").strip()
            m["procedure"] = (row.get(_PROC_COL) or "").strip()

    entries = []
    for (rex, full), counter in descs.items():
        m = meta[(rex, full)]
        entries.append({
            "rex": rex,
            "general_code": full[:8],
            "full_code": full,
            "taric_code": full[8:10],
            "description": choose_description(counter),
            "origin": m["origin"],
            "preference": m["preference"],
            "procedure": m["procedure"],
            "lines": m["lines"],
            "last_used": m["last_used"],
        })
    entries.sort(key=lambda e: (e["rex"], e["full_code"]))
    stats["entries"] = len(entries)
    stats["clients"] = len({e["rex"] for e in entries if e["rex"]})
    return entries, stats


def parse_export(source) -> tuple[list[dict], dict]:
    """read_export + build_list in one call."""
    return build_list(read_export(source))


# ── The derived list, as committed to the repo ──────────────────────────
DERIVED_COLUMNS = ["rex", "general_code", "full_code", "taric_code",
                   "description", "origin", "preference", "procedure",
                   "lines", "last_used"]


def write_derived(entries: list[dict], path) -> None:
    """Write the folded list as UTF-8 CSV (the file the loader reads)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=DERIVED_COLUMNS)
        w.writeheader()
        for e in entries:
            w.writerow({k: e.get(k, "") for k in DERIVED_COLUMNS})


def read_derived(path) -> list[dict]:
    """Read the committed derived list back."""
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["lines"] = int(r.get("lines") or 0)
    return rows


def group_by_rex(entries: list[dict]) -> dict[str, list[dict]]:
    """REX -> its list entries ('' = lines that declared no proof of origin)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        out[e.get("rex", "")].append(e)
    return dict(out)


def merge_by_code(entries: list[dict]) -> list[dict]:
    """Collapse entries from several REX groups into one list per full code.

    The company-wide list holds one row per code, but the same code can
    appear under many exporters in the export. The wording backed by the
    most declared lines wins.
    """
    best: dict[str, dict] = {}
    for e in entries:
        cur = best.get(e["full_code"])
        if cur is None or e.get("lines", 0) > cur.get("lines", 0):
            merged = dict(e)
            merged["lines"] = (cur or {}).get("lines", 0) + e.get("lines", 0)
            best[e["full_code"]] = merged
        else:
            cur["lines"] = cur.get("lines", 0) + e.get("lines", 0)
    return sorted(best.values(), key=lambda e: e["full_code"])


def product_payload(entry: dict) -> dict:
    """The `commodity_codes` columns for one entry (provenance dropped)."""
    return {
        "general_code": entry["general_code"],
        "full_code": entry["full_code"],
        "taric_code": entry.get("taric_code") or entry["full_code"][8:10],
        "description": entry.get("description") or "",
    }


# ── A plain code list (spreadsheet), merged into the derived list ───────
# A CDS Items export is one row per declared goods line; a hand-kept or
# exported code list is one row per code, with no provenance:
#
#     Commodity Code | Additional Taric | Description
#     02013000       | 90               | TARTARE DI FASONA
#
# Both shapes end in the same list, so a spreadsheet can top up the list
# with codes the export never carried.
_LIST_CODE_HEADERS = ("commodity code", "code", "full code", "general code")
_LIST_TARIC_HEADERS = ("additional taric", "taric", "taric code", "additional")
_LIST_DESC_HEADERS = ("description", "commodity description", "goods")


def _header_index(header: list[str], names: tuple[str, ...]) -> int:
    """Position of the first column whose name matches, or -1."""
    lowered = [re.sub(r"\s+", " ", str(h or "")).strip().casefold()
               for h in header]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return -1


# An .xlsx is a zip; sniffing the magic beats trusting an upload's filename.
_XLSX_MAGIC = b"PK\x03\x04"


def _list_rows(source) -> list[list]:
    """Rows of a code list — path or bytes, .xlsx or CSV — as raw cell lists."""
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        with open(source, "rb") as fh:
            raw = fh.read()
    if raw.startswith(_XLSX_MAGIC):
        import openpyxl                                   # optional at import
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True,
                                    read_only=True)
        try:
            return [list(r) for r in wb.active.iter_rows(values_only=True)]
        finally:
            wb.close()
    return list(csv.reader(io.StringIO(_decode(raw), newline="")))


def read_code_list(source) -> tuple[list[dict], dict]:
    """Read a one-row-per-code list (path or uploaded bytes) into list entries.

    Returns (entries, stats). Entries carry no provenance — no REX, no
    origin, `lines` 0 and no `last_used` — because a code list records
    that a code is used, not any declaration it came from. Rows whose code
    is not a full 8-digit one (a 6-digit stub such as '852910') are counted
    in `stats['skipped_no_code']` and left out, exactly as `build_list`
    leaves them out of the export: padding a stub would invent a code that
    was never declared.
    """
    rows = _list_rows(source)
    stats = {"rows": 0, "skipped_no_code": 0, "skipped": []}
    if not rows:
        stats["entries"] = 0
        return [], stats

    header, *body = rows
    i_code = _header_index(header, _LIST_CODE_HEADERS)
    i_taric = _header_index(header, _LIST_TARIC_HEADERS)
    i_desc = _header_index(header, _LIST_DESC_HEADERS)
    if i_code < 0 or i_desc < 0:
        raise ValueError(
            "The sheet needs a commodity-code column and a description "
            f"column; found {[str(h) for h in header if h] or 'no headers'}")

    def cell(row, i):
        return "" if i < 0 or i >= len(row) or row[i] is None else str(row[i])

    seen: dict[str, dict] = {}
    for row in body:
        if not any(str(c or "").strip() for c in row):
            continue
        stats["rows"] += 1
        raw_code, raw_taric = cell(row, i_code).strip(), cell(row, i_taric)
        general, taric = split_code(
            raw_code if "/" in raw_code else f"{raw_code}/{raw_taric}")
        desc = clean_description(cell(row, i_desc))
        if not general or not desc:
            stats["skipped_no_code"] += 1
            stats["skipped"].append((raw_code, raw_taric.strip(), desc))
            continue
        full = general + taric
        # The same code twice in one sheet is one entry, first wording wins.
        seen.setdefault(full, {
            "rex": "",
            "general_code": general,
            "full_code": full,
            "taric_code": taric,
            "description": desc,
            "origin": "",
            "preference": "",
            "procedure": "",
            "lines": 0,
            "last_used": "",
        })
    entries = sorted(seen.values(), key=lambda e: e["full_code"])
    stats["entries"] = len(entries)
    return entries, stats


def add_entries(existing: list[dict], additions: list[dict]) -> tuple[list[dict], dict]:
    """Add only the codes the list does not already hold.

    A code already in `existing` under ANY exporter REX is already in the
    company-wide list (the loader folds every REX group to one row per
    code), so it is never appended a second time — and its wording, backed
    by real declared lines, is left alone. Returns (entries, stats) with
    the merged list sorted the way `write_derived` expects.
    """
    have = {e["full_code"] for e in existing}
    stats = {"added": 0, "already_present": 0}
    merged = list(existing)
    for entry in additions:
        if entry["full_code"] in have:
            stats["already_present"] += 1
            continue
        have.add(entry["full_code"])
        merged.append(dict(entry))
        stats["added"] += 1
    merged.sort(key=lambda e: (e.get("rex", ""), e["full_code"]))
    return merged, stats
