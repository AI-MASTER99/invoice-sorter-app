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
         claiming preference 300. Reference = the supplier's REX number
         (digits only, prefix stripped); left blank when no REX is known
         so the gap surfaces to the reviewer (never the invoice number —
         deliberate, colleague-confirmed divergence from gov.uk guidance).
  List documents — product-specific docs from the client's V-lookup list
         (client_products.documents) follow the always-present ones.
  N853 — CHED-P veterinary certificate. The operator's rule: required
         whenever the commodity code starts with an animal-origin prefix
         (see _N853_PREFIXES). The BTOM low-risk-dairy nuance is NOT yet
         confirmed with the customs specialist, so per "never guess" this
         engine does not ADD the document — it FLAGS lines that look like
         they need N853 but don't carry it in their list docs.

Slot budget: the template has 3 document slots. This module returns the
full ordered list; the caller (build_items_xlsx) writes the first three
and stamps a NOT-DECLARED marker for any overflow — never a silent drop.
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


def _norm_digits(code: str) -> str:
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


def n853_required(code: str) -> bool:
    """Operator's rule: animal-origin prefix → CHED-P (N853) is expected."""
    digits = _norm_digits(code)
    return len(digits) >= 3 and digits[:3] in _N853_PREFIXES


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
        # U116 reference: only the number AFTER the country/"REX" prefix
        # (ITREXIT06167560157 -> 06167560157); blank when unknown so the
        # gap surfaces (as_text at the write site preserves leading zeros).
        docs.append({"code": "U116", "id": re.sub(r"^[A-Za-z]+", "", rex_ref or ""),
                     "status": "JE", "reason": ""})
    docs += list_docs

    flags: list[str] = []
    declared = {(d.get("code") or "").upper() for d in docs}
    if n853_required(code8) and "N853" not in declared:
        # Deliberately a FLAG, not an auto-added doc: the CHED reference is
        # shipment-specific (not derivable from the invoice), and the BTOM
        # low-risk exemption nuance is unconfirmed. Never guess.
        flags.append(
            f"N853 (CHED-P) is normally required for {code8[:3]}… animal-origin "
            f"codes but is not in the client list for this product — confirm "
            f"and add the CHED reference manually"
        )
    return docs, flags
