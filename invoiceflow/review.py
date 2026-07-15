"""Issue detector for the invoice review screen — the single source of truth
for clear, located, plain-language problem messages.

This module is deliberately PURE: it imports nothing from the app, the
database, or the network. It takes the plain dicts/lists the pipeline already
computes (the rows, the totals cross-check, the A/B disagreement flags) and
returns a list of issues that the /review endpoint and the frontend can render
directly. Being import-free means it can be unit-tested in complete isolation.

Each issue is a dict:
    {
      "severity": "high" | "medium" | "info",
      "location": e.g. "Line 3" | "Totals" | "Whole invoice",
      "field":    e.g. "net weight" | "commodity code",
      "expected": what the invoice / list said (string, may be ""),
      "found":    what the app computed/extracted (string, may be ""),
      "message":  full plain-language sentence describing the problem,
      "action":   what the human should do about it,
    }
"""
from __future__ import annotations

import re

# Fee/charge rows (transport, packaging, CONAI, insurance…) legitimately have
# no commodity code, origin or weight — so the field/weight checks must SKIP
# them. main.py's _is_fee_row delegates to this one (no duplication).
#
# Two classes of keyword, matched differently so we neither under- nor
# over-match:
#  • _FEE_WORDS — short English terms that must match as WHOLE words (with an
#    optional plural "s"). A naive substring match flagged "COFFEE" via "fee"
#    and dropped a real goods line; whole-word matching also spares "charger"
#    / "discharge" from "charge".
#  • _FEE_STEMS — longer, language-specific stems matched as a word PREFIX so
#    variants are caught ("verzend" → "verzendkosten", "imballo" →
#    "imballaggio"). These are specific enough not to hit goods descriptions.
_FEE_WORDS = ("fee", "charge", "stamp")
_FEE_STEMS = (
    "contributo", "spese", "transport", "trasporto", "insurance",
    "assicurazione", "certificate", "certificato", "bollo",
    "handling", "imballo", "imballaggio", "packaging", "delivery",
    "consegna", "frais", "gebühr", "verzend",
)
_FEE_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, _FEE_WORDS)) + r")s?\b"
    r"|\b(?:" + "|".join(map(re.escape, _FEE_STEMS)) + r")",
    re.IGNORECASE | re.UNICODE,
)

# ISO 3166-1 alpha-2 country codes (+ XI for Northern Ireland, used in CDS).
_ISO2 = frozenset((
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ "
    "BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR "
    "CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU "
    "ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ "
    "LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ "
    "MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF "
    "PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI "
    "SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR "
    "TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XI YE YT ZA ZM ZW"
).split())


def _is_fee_row(description: str) -> bool:
    return bool(description) and _FEE_RE.search(description) is not None


# Friendly labels + units for the four totals keys produced by compare_totals().
_TOTAL_LABELS = {
    "total_packages": "Number of packages",
    "total_gross_kg": "Gross weight (kg)",
    "total_net_kg": "Net weight (kg)",
    "total_value": "Total value",
}
_TOTAL_UNITS = {
    "total_packages": "",
    "total_gross_kg": " kg",
    "total_net_kg": " kg",
    "total_value": "",
}

# Friendly labels for the per-row data columns (mirrors find_cell_disagreements).
_COL_LABELS = {
    "Comm./imp. cod": "commodity code",
    "Description of Goods": "description",
    "Origin": "origin",
    "Country": "country",
    "Number of Packages": "number of packages",
    "Gross Weight (KG)": "gross weight",
    "Net Weight (KG)": "net weight",
    "Value": "value",
}

# Marker written onto a row's description when its product is not in the
# client list (see Spoor C, step 10). Kept here so the detector and the
# pipeline agree on one spelling.
NOT_IN_LIST_MARKER = "*** NOT IN LIST ***"

_SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}


def _to_float(s):
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _fmt(n: float) -> str:
    """Whole numbers as integers (packages), otherwise two decimals."""
    if abs(n - round(n)) < 0.005:
        return str(int(round(n)))
    return f"{n:.2f}"


def _fmt_signed(n: float) -> str:
    return ("+" if n >= 0 else "-") + _fmt(abs(n))


def _issue(severity, location, field, message, action, expected="", found=""):
    return {
        "severity": severity,
        "location": location,
        "field": field,
        "expected": expected,
        "found": found,
        "message": message,
        "action": action,
    }


def check_totals(totals_check: dict | None) -> list[dict]:
    """Strict reconciliation of the four totals against the invoice.

    Rule (confirmed by the user): there is NO tolerance — any real difference
    is an error. Only pure floating-point rounding noise is absorbed (we round
    both sides to 2 decimals before comparing). Each total is judged
    independently and gets its own message. `totals_check` is the dict produced
    by compare_totals(): {key: {"reported", "computed", "match"}}.
    """
    issues: list[dict] = []
    for key, label in _TOTAL_LABELS.items():
        c = (totals_check or {}).get(key)
        if not c:
            continue
        reported, computed = c.get("reported", ""), c.get("computed", "")
        unit = _TOTAL_UNITS[key]
        rf, cf = _to_float(reported), _to_float(computed)
        if rf is None or cf is None:
            # Nothing to compare against (e.g. no total printed on the invoice).
            issues.append(_issue(
                "info", "Totals", label.lower(),
                f"Could not check {label.lower()} — no total found on the "
                f"invoice to compare against.",
                f"Verify the {label.lower()} manually.",
                expected=str(reported), found=str(computed),
            ))
            continue
        # Absorb pure float rounding only; any real difference is an error.
        if round(rf, 2) != round(cf, 2):
            diff = round(cf - rf, 2)
            issues.append(_issue(
                "high", "Totals", label.lower(),
                f"{label} does not match the invoice: the lines add up to "
                f"{_fmt(cf)}{unit}, but the invoice total is {_fmt(rf)}{unit} "
                f"(difference {_fmt_signed(diff)}{unit}). Something was read "
                f"wrong on one of the lines.",
                f"Check the {label.lower()} on the lines against the invoice.",
                expected=f"{_fmt(rf)}{unit}", found=f"{_fmt(cf)}{unit}",
            ))
    return issues


def _row_name(row: dict) -> str:
    desc = (row.get("Description of Goods", "") or "").strip()
    if desc.startswith(NOT_IN_LIST_MARKER):
        desc = desc[len(NOT_IN_LIST_MARKER):].strip()
    return desc or "(no description)"


def check_rows(rows: list[dict] | None, flagged_cells: list | None = None) -> list[dict]:
    """Per-line problems: products not in the client list, and cells where the
    two independent readings disagree (the 'uncertain' yellow cells)."""
    issues: list[dict] = []
    rows = rows or []
    flagged_cells = flagged_cells or []
    for i, row in enumerate(rows):
        line = i + 1
        name = _row_name(row)
        code = (row.get("Comm./imp. cod", "") or "").strip()
        desc = (row.get("Description of Goods", "") or "").strip()
        not_in_list = bool(row.get("_not_in_list")) or desc.startswith(NOT_IN_LIST_MARKER)
        if not_in_list:
            issues.append(_issue(
                "high", f"Line {line}", "commodity code",
                f"Line {line} '{name}' is not in the client list "
                f"(invoice code {code or 'unknown'}). Its code and description "
                f"could not be filled from the list.",
                "Look up the correct full commodity code and add the product "
                "to the client list.",
                expected="a match in the client list", found="not found",
            ))
            # A not-in-list row is already the headline problem for that line;
            # don't pile on uncertain-cell noise for it.
            continue
        cells = flagged_cells[i] if i < len(flagged_cells) else None
        if cells:
            labels = ", ".join(sorted(_COL_LABELS.get(c, c) for c in cells))
            issues.append(_issue(
                "medium", f"Line {line}", labels,
                f"Line {line} '{name}': the two independent readings disagree "
                f"on {labels}. This value is uncertain.",
                "Check these fields on the invoice and correct if needed.",
            ))
    return issues


def check_structure(ab_reasons: list | None) -> list[dict]:
    """Whole-invoice structural problems detected by rows_match(), e.g. the two
    readings produced a different number of rows."""
    issues: list[dict] = []
    for reason in ab_reasons or []:
        if str(reason).lower().startswith("row count differs"):
            issues.append(_issue(
                "high", "Whole invoice", "row count",
                f"The two independent readings produced a different number of "
                f"rows. {reason}",
                "Re-check the invoice — a line may have been missed or duplicated.",
            ))
    return issues


def _num(s):
    if s is None or s == "":
        return None
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return None


def _lines(nums: list[int]) -> str:
    """Human list: 'line 3' / 'lines 3 and 7' / 'lines 3, 7 and 12'."""
    nums = sorted(set(nums))
    if len(nums) == 1:
        return f"line {nums[0]}"
    if len(nums) == 2:
        return f"lines {nums[0]} and {nums[1]}"
    return "lines " + ", ".join(str(n) for n in nums[:-1]) + f" and {nums[-1]}"


def _loc(nums: list[int]) -> str:
    nums = sorted(set(nums))
    return (f"Line {nums[0]}" if len(nums) == 1 else f"Lines {nums[0]}…")


def _currency_symbol(value_str) -> str:
    """The leading non-numeric prefix of a value like '€46.20' -> '€'."""
    m = re.match(r"^\s*([^\d\-.,\s]+)", str(value_str or ""))
    return m.group(1) if m else ""


# Symbols and ISO codes are the same currency — canonicalize before comparing,
# so a line in '€' and a total in 'EUR' don't look like a mismatch.
_CCY_CANON = {
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "$": "USD", "USD": "USD", "US$": "USD",
    "CHF": "CHF", "FR": "CHF",
}


def _canon_ccy(s: str) -> str:
    if not s:
        return s
    return _CCY_CANON.get(s) or _CCY_CANON.get(s.upper(), s)


def check_currency(rows, totals) -> list[dict]:
    """Every line value must be in the same currency, and that currency must
    match the invoice total's currency. Symbols and ISO codes (€ vs EUR) are
    treated as the same currency."""
    if not rows:
        return []
    syms = {c for r in (rows or []) if (c := _canon_ccy(_currency_symbol(r.get("Value"))))}
    total_sym = _canon_ccy(_currency_symbol((totals or {}).get("total_value_raw")))
    if len(syms) > 1:
        return [_issue("high", "Currency", "currency",
            f"The invoice mixes more than one currency ({', '.join(sorted(syms))}). "
            f"Every line should be in one currency.",
            "Check the currency on each line.")]
    if syms and total_sym and total_sym not in syms:
        only = next(iter(syms))
        return [_issue("high", "Currency", "currency",
            f"The line currency ({only}) does not match the invoice total "
            f"currency ({total_sym}).",
            "Check which currency is correct.",
            expected=total_sym, found=only)]
    if not syms:
        return [_issue("info", "Currency", "currency",
            "No currency symbol was detected on the line values.",
            "Confirm the invoice currency manually.")]
    return []


def check_weights(rows) -> list[dict]:
    """Goods lines: net <= gross, and gross/net present & positive."""
    over, miss_gross, miss_net = [], [], []
    for i, row in enumerate(rows or []):
        if _is_fee_row(row.get("Description of Goods", "")):
            continue
        line = i + 1
        g = _num(row.get("Gross Weight (KG)"))
        n = _num(row.get("Net Weight (KG)"))
        if g is None or g <= 0:
            miss_gross.append(line)
        if n is None or n <= 0:
            miss_net.append(line)
        if g is not None and n is not None and n > g + 1e-6:
            over.append(line)
    issues = []
    if over:
        issues.append(_issue("high", _loc(over), "weights",
            f"Net weight is greater than gross weight on {_lines(over)} — "
            f"that is impossible.",
            "Check the gross and net weights on those lines."))
    if miss_gross:
        issues.append(_issue("high", _loc(miss_gross), "gross weight",
            f"Gross weight is missing or zero on {_lines(miss_gross)}.",
            "Fill in the gross weight."))
    if miss_net:
        issues.append(_issue("high", _loc(miss_net), "net weight",
            f"Net weight is missing or zero on {_lines(miss_net)}.",
            "Fill in the net weight."))
    return issues


def check_fields(rows) -> list[dict]:
    """Goods lines: origin present + valid code, commodity code present + right
    length, value present; plus genuinely empty rows. Fee rows are skipped."""
    miss_origin, bad_origin = [], []
    miss_code, bad_code = [], []
    odd_code = []   # 7/9 digits — almost always a dropped leading zero
    miss_value, empty_rows = [], []
    for i, row in enumerate(rows or []):
        line = i + 1
        desc = (row.get("Description of Goods", "") or "").strip()
        code = re.sub(r"\D", "", row.get("Comm./imp. cod", "") or "")
        origin = (row.get("Origin", "") or "").strip().upper()
        value = (row.get("Value", "") or "").strip()
        if not desc and not code and not value:
            empty_rows.append(line)
            continue
        if _is_fee_row(desc):
            continue
        not_in_list = desc.startswith(NOT_IN_LIST_MARKER) or bool(row.get("_not_in_list"))
        if not origin:
            miss_origin.append(line)
        elif origin not in _ISO2:
            bad_origin.append(line)
        # Odd-length codes (7/9 digits) are flagged for EVERY row, including
        # NOT-IN-LIST ones: real codes are even-length, so this is almost
        # always Excel/AI dropping the leading zero of a chapter 01-09 code
        # (04061030 → 4061030). Downstream classification assumes the zero
        # back, but the human must confirm — never a silent repair.
        if code and len(code) >= 5 and len(code) % 2 == 1:
            odd_code.append(line)
        if not not_in_list:
            if not code:
                miss_code.append(line)
            elif not (8 <= len(code) <= 10):
                bad_code.append(line)
        if not value:
            miss_value.append(line)
    issues = []
    if miss_origin:
        issues.append(_issue("high", _loc(miss_origin), "origin",
            f"Country of origin is missing on {_lines(miss_origin)}.",
            "Fill in the country of origin."))
    if bad_origin:
        issues.append(_issue("high", _loc(bad_origin), "origin",
            f"Country of origin is not a valid country code on {_lines(bad_origin)}.",
            "Correct the country of origin (a 2-letter code like IT, ES)."))
    if miss_code:
        issues.append(_issue("high", _loc(miss_code), "commodity code",
            f"Commodity code is missing on {_lines(miss_code)}.",
            "Fill in the commodity code."))
    if bad_code:
        issues.append(_issue("high", _loc(bad_code), "commodity code",
            f"Commodity code has an unexpected length on {_lines(bad_code)} "
            f"(should be 8–10 digits).",
            "Check the commodity code."))
    if odd_code:
        issues.append(_issue("high", _loc(odd_code), "commodity code",
            f"Commodity code on {_lines(odd_code)} has an odd number of digits "
            f"— a leading zero was probably lost (e.g. 4061030 should be "
            f"04061030). The export assumes the zero back, but you must "
            f"confirm the code is right.",
            "Verify the commodity code and add the missing leading zero."))
    if miss_value:
        issues.append(_issue("high", _loc(miss_value), "value",
            f"Value is missing on {_lines(miss_value)}.",
            "Fill in the line value."))
    if empty_rows:
        issues.append(_issue("high", _loc(empty_rows), "empty row",
            f"{_lines(empty_rows).capitalize()} appear(s) to be empty.",
            "Remove the empty line or fill it in."))
    return issues


def build_review_issues(rows=None, totals=None, totals_check=None,
                        ab_reasons=None, flagged_cells=None, tariff_data=None):
    """Single source of truth for the review screen's problem list.

    Runs the full Spoor-B validation: structure, totals reconciliation,
    currency, weights, per-field sanity, and per-line (not-in-list / uncertain)
    checks. All inputs are values the pipeline already computes/stores;
    `tariff_data` is accepted for forward-compatibility. Returns the issues
    sorted high -> medium -> info.
    """
    issues: list[dict] = []
    issues += check_structure(ab_reasons)
    issues += check_totals(totals_check)
    issues += check_currency(rows, totals)
    issues += check_weights(rows)
    issues += check_fields(rows)
    issues += check_rows(rows, flagged_cells)
    issues.sort(key=lambda x: _SEVERITY_ORDER.get(x["severity"], 9))
    return issues


def summarize(issues: list[dict]) -> dict:
    """Compact status for the review-screen header banner."""
    high = sum(1 for i in issues if i["severity"] == "high")
    medium = sum(1 for i in issues if i["severity"] == "medium")
    info = sum(1 for i in issues if i["severity"] == "info")
    needs_review = (high + medium) > 0
    return {
        "status": "needs_review" if needs_review else "verified",
        "high": high,
        "medium": medium,
        "info": info,
        "total": len(issues),
    }


def review_payload(invoice: dict | None) -> dict:
    """Map a stored invoice record (what db.get_invoice() returns) to the
    review-screen payload: {summary, issues}.

    Pure and defensive: tolerates missing keys so it works on older invoices
    that may not have every field. `flagged_cells` is read if present (the
    pipeline can persist it later) — its absence simply means no
    uncertain-cell issues are shown.
    """
    invoice = invoice or {}
    # flagged_cells are nested inside `totals` (the invoices table has no
    # dedicated column); fall back to a top-level key for forward-compat if
    # a real column is ever added.
    _totals = invoice.get("totals") or {}
    flagged = invoice.get("flagged_cells")
    if flagged is None and isinstance(_totals, dict):
        flagged = _totals.get("_flagged_cells")
    issues = build_review_issues(
        rows=invoice.get("rows") or [],
        totals=_totals,
        totals_check=invoice.get("totals_check"),
        ab_reasons=invoice.get("ab_reasons") or [],
        flagged_cells=flagged,
        tariff_data=invoice.get("tariff_data"),
    )
    return {"summary": summarize(issues), "issues": issues}
