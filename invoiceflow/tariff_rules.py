"""
Per-commodity-code document rules for the MultiFreight CDS Items export.

Phase 4 of docs/multifreight_rules_engine_plan.md — the deterministic,
locally-evaluated rule layer. Firm constraints (from the plan):

  * NEVER guess. Anything not determinable from the invoice + the client
    list is returned as a FLAG for a human, not silently filled or dropped.
  * Rules here are the ones confirmed deterministic today. The API-driven
    rule authoring (tariff_cache / tariff_reference / client_tariff_overrides,
    migration 005) plugs in behind this same interface later.

Rules implemented:

  N935 — commercial invoice document. Always present (slot 01), id "VM1
         <invoice number>", status JE.
  Y929 — "not organic" exemption (Reg. 834/2007). CONDITIONAL: organic
         certification only concerns agri-food goods, so it is only
         declared for food chapters 01-24. Declaring it on e.g. an
         escalator (8428…) was noise — previously it was unconditional.
  U116 — TCA proof-of-origin (statement on origin) for EU-origin lines
         claiming preference 300. Reference = the supplier's FULL REX code
         including prefix (ITREXIT06167560157 — operator-requested, 2026-07);
         left blank when no REX is known so the gap surfaces to the reviewer
         (never the invoice number — deliberate, colleague-confirmed
         divergence from gov.uk guidance).
  List documents — product-specific docs from the company's V-lookup list
         (commodity_codes.documents) follow the always-present ones.
  N853 — CHED-P veterinary certificate. The operator's rule: required
         whenever the commodity code starts with an animal-origin prefix
         (see _N853_PREFIXES). The BTOM low-risk-dairy nuance is NOT yet
         confirmed with the customs specialist, so per "never guess" this
         engine does not ADD the document — it FLAGS lines that look like
         they need N853 but don't carry it in their list docs.

Slot budget: the export sheet carries 6 document slots (the stock
template's 3 are widened by main._ensure_document_slots). This module
returns the full ordered list; the caller (build_items_xlsx) writes the
first six and stamps a NOT-DECLARED marker for any overflow — never a
silent drop.
"""
from __future__ import annotations

import re

# First-3-digit commodity-code prefixes with animal-origin content, per the
# operator's confirmed working rule ("trucje"). Covers meat (020/021), fish
# (030), dairy (040/041), other animal products (050), animal fats (150/152),
# meat/fish preparations (160), misc edible preparations containing animal
# product (210), animal feed (230), albumins/caseins (350), hides (410),
# furskins (510) and feather/down articles (670).
_N853_PREFIXES = frozenset({
    "020", "021", "030", "040", "041", "050", "150", "152",
    "160", "210", "230", "350", "410", "510", "670",
})

# Y929 (organic-regulation exemption) is only meaningful for agri-food:
# chapters 01-24.
_FOOD_CHAPTER_MAX = 24

# Chapters inside that range that VATZ is NOT defaulted onto. Chapter 22
# (beverages, spirits and vinegar) is overwhelmingly standard-rated drink.
# A frozenset rather than `!= 22` because the same question is open for
# other pockets of the range (ch. 24 tobacco, confectionery in 1704/1806).
_VATZ_EXCLUDED_CHAPTERS = frozenset({22})


def norm_digits(code: str) -> str:
    """Digits only, with a dropped leading zero restored.

    Commodity codes are even-length (6/8/10). Excel and the AI routinely
    drop the leading zero on chapters 01-09, yielding an ODD length
    (e.g. 04061030 -> 4061030). Left-pad odd lengths so the chapter/prefix
    read off the front is correct — otherwise `4061030` parses as chapter
    40 (rubber) instead of 04 (dairy), silently skipping Y929 and MISSING
    the N853 vet-cert flag. Mirrors _norm_general_code's zfill in main.py.
    """
    digits = re.sub(r"\D", "", code or "")
    # Real codes are even-length (6/8/10); a dropped leading zero makes them
    # odd (5/7/9). Restore it only for those plausible lengths — never pad a
    # 1- or 3-digit stray into a spurious chapter.
    if len(digits) >= 5 and len(digits) % 2 == 1:
        digits = "0" + digits
    return digits


_norm_digits = norm_digits          # internal alias (pre-existing callers)


def split_commodity_code(code: str) -> tuple[str, str, bool]:
    """Split a declared code into its two customs parts.

    A commodity code is 8 digits of nomenclature (the HS/CN code) plus a
    2-digit additional TARIC code — "0704901000" is 07049010 + 00. Customs
    reads them as two boxes, so both the MultiFreight Items file and the
    working sheet write them as two columns.

    Returns (code8, taric, zero_assumed). `zero_assumed` reports a repaired
    leading zero (see norm_digits); the repair is never silent — the caller
    stamps a VERIFY marker on the line. Anything shorter than 8 digits after
    the repair cannot be split (an internal SKU, a truncated entry): both
    parts come back empty so the caller shows the value untouched.
    """
    raw = re.sub(r"\D", "", code or "")
    digits = norm_digits(code)
    zero_assumed = digits != raw
    if len(digits) < 8:
        return "", "", zero_assumed
    # Every digit past the 8-digit code: a 10-digit code gives "00".."99",
    # and a stray 11th digit stays visible rather than being trimmed away.
    return digits[:8], digits[8:], zero_assumed


def _chapter(code: str) -> int | None:
    """First two digits of a commodity code, or None if not derivable."""
    digits = _norm_digits(code)
    if len(digits) < 2:
        return None
    try:
        return int(digits[:2])
    except ValueError:
        return None


def y929_applies(code: str) -> bool:
    """Y929 ('not organic') belongs on agri-food lines only (ch. 01-24)."""
    ch = _chapter(code)
    return ch is not None and 1 <= ch <= _FOOD_CHAPTER_MAX


def vatz_applies(code: str) -> bool:
    """VATZ (zero-rated VAT): the food chapters (01-24) except chapter 22.

    VATZ used to go on EVERY goods line (operator instruction 2026-07).
    That is wrong for the bulk of the list: zero-rating is a food relief,
    and 1110 of the 1720 codes are outside the food chapters.

    Chapter 22 is then subtracted. It is a chapter test, not UK VAT law,
    and chapter 22 is the place the two diverge worst: "beverages, spirits
    and vinegar" sits inside the agri-food range while most of it is
    standard-rated drink. Asked whether to drop the whole chapter or
    correct it per code, the customs specialist answered "may be best to
    exclude" (2026-10), so the chapter is out. Two consequences worth
    knowing before this is widened or narrowed again:

      * the genuinely zero-rated minority of the chapter (vinegar 2209,
        plant-milk drinks) loses VATZ with it. Leaving the list blank does
        NOT give it back — a blank `nat_add_code` means "use this default",
        and for chapter 22 the default is now nothing. Only a value in the
        cell is written, so those codes need the literal "VATZ" typed into
        the list's National Additional Codes column, through the sheet
        import (the single-code form does not carry the field). See
        `_items_workbook` in main.py.
      * he also wrote "we have very few VATZ on our imports … VATZ is not
        required", which reads wider than one chapter. That is a separate,
        unanswered question; the list is company-wide, so dropping the
        default outright would also strip VATZ from another client's food
        business, where it is correct.

    Y929 is deliberately NOT narrowed the same way: wine IS covered by
    Reg. 834/2007, so the organic exemption still belongs on ch. 22 lines.
    """
    return y929_applies(code) and _chapter(code) not in _VATZ_EXCLUDED_CHAPTERS


def n853_required(code: str) -> bool:
    """Operator's rule: animal-origin prefix → CHED-P (N853) is expected."""
    digits = _norm_digits(code)
    return len(digits) >= 3 and digits[:3] in _N853_PREFIXES


# The documents resolve_line_docs puts on EVERY line it applies to, so a
# commodity-code list must NOT also carry them: the export would declare
# each one twice and burn two of the six document slots.
AUTO_LINE_DOCS = frozenset({"N935", "Y929", "U116"})


def resolve_line_docs(
    *,
    code8: str,
    is_eu_origin: bool,
    invoice_number: str = "",
    rex_ref: str = "",
    list_docs: list[dict] | None = None,
) -> tuple[list[dict], list[str]]:
    """Build the ordered DE 2/3 document list + human-review flags for one line.

    Returns (docs, flags):
      docs  — [{code, id, status, reason}, …] in deterministic slot order:
              N935, [Y929 if food], [U116 if EU], then the client-list docs.
      flags — plain-language issues the reviewer must resolve (e.g. a
              probably-required N853 that is not in the list). The caller
              surfaces these on the line; they are never silently dropped.
    """
    list_docs = list(list_docs or [])
    docs: list[dict] = [
        {"code": "N935", "id": f"VM1 {invoice_number}".strip(),
         "status": "JE", "reason": ""},
    ]
    if y929_applies(code8):
        docs.append({"code": "Y929", "id": "Excluded from regulation 834/2007",
                     "status": "", "reason": "Excluded from regulation 834/2007"})
    if is_eu_origin:
        # U116 reference: the FULL REX code including its prefix
        # (ITREXIT06167560157) — per the customs operator's explicit request
        # (2026-07: MultiFreight column BT must show the complete code, not
        # just the digits). Blank when unknown so the gap surfaces.
        docs.append({"code": "U116", "id": (rex_ref or "").strip(),
                     "status": "JE", "reason": ""})
    docs += list_docs

    flags: list[str] = []
    declared = {(d.get("code") or "").upper() for d in docs}
    if n853_required(code8) and "N853" not in declared:
        # Deliberately a FLAG, not an auto-added doc: the CHED reference is
        # shipment-specific (not derivable from the invoice), and the BTOM
        # low-risk exemption nuance is unconfirmed. Never guess.
        # Short on purpose: this text lands on the export line next to the
        # product description, which must stay readable (operator feedback:
        # a long prefix read as the description being "replaced").
        flags.append("CHECK: N853/CHED-P may be required — not in client list")
    return docs, flags
