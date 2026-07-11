"""Unit tests for tariff_rules — the Phase-4/5 rule engine.

Covers the plan's Phase-5 checklist: food code with the N853-vs-list
conditional, non-food code (no Y929), EU vs non-EU (U116), unknown/short
code, and the >3-docs overflow contract (engine returns everything in
order; the caller slices + flags).

Run: python -m pytest tests_tariff_rules.py -q
"""
import tariff_rules as tr


# ── Y929: food chapters only ────────────────────────────────────────────
def test_y929_applies_to_food_chapters():
    assert tr.y929_applies("04061030")      # dairy, ch. 04
    assert tr.y929_applies("16041311")      # fish preparations, ch. 16
    assert tr.y929_applies("24021000")      # tobacco, ch. 24 (upper bound)


def test_y929_not_on_non_food():
    assert not tr.y929_applies("84282000")  # escalators/lifts, ch. 84
    assert not tr.y929_applies("25010091")  # salt, ch. 25 (just past bound)
    assert not tr.y929_applies("")          # no code → no claim
    assert not tr.y929_applies("4")         # too short to derive a chapter


# ── N853: animal-origin prefixes ────────────────────────────────────────
def test_n853_required_for_animal_prefixes():
    assert tr.n853_required("02013000")     # 020 meat
    assert tr.n853_required("04061030")     # 040 dairy
    assert tr.n853_required("03028990")     # 030 fish
    assert tr.n853_required("16041311")     # 160 preparations
    assert tr.n853_required("23099010")     # 230 animal feed


def test_n853_not_required_elsewhere():
    assert not tr.n853_required("07094000")  # vegetables (070)
    assert not tr.n853_required("84282000")  # machinery
    assert not tr.n853_required("")          # no code
    assert not tr.n853_required("02")        # too short for a prefix


# ── Dropped-leading-zero normalization (the HIGH finding) ────────────────
def test_dropped_leading_zero_still_classifies_dairy():
    # 04061030 with the leading zero dropped by Excel/AI → "4061030"
    assert tr.n853_required("4061030")      # must still flag (was missed)
    assert tr.y929_applies("4061030")       # must still be food ch.04
    assert tr._chapter("4061030") == 4      # not 40 (rubber)


def test_dropped_leading_zero_meat_and_fish():
    assert tr.n853_required("2013000")      # 02013000 meat
    assert tr.n853_required("3028990")      # 03028990 fish
    # a genuine even-length non-animal code is untouched
    assert not tr.n853_required("84282000")


# ── resolve_line_docs: composition ──────────────────────────────────────
def _codes(docs):
    return [d["code"] for d in docs]


def test_food_eu_line_gets_n935_y929_u116():
    docs, flags = tr.resolve_line_docs(
        code8="07049010", is_eu_origin=True,
        invoice_number="INV-1", rex_ref="ITREXIT06167560157")
    assert _codes(docs) == ["N935", "Y929", "U116"]
    assert docs[0]["id"] == "VM1 INV-1"
    # REX prefix stripped, digits kept (incl. leading zero)
    assert docs[2]["id"] == "06167560157"
    assert flags == []  # 070 is not an animal prefix


def test_non_food_line_has_no_y929():
    docs, flags = tr.resolve_line_docs(
        code8="84282000", is_eu_origin=True,
        invoice_number="INV-2", rex_ref="ITREXIT06167560157")
    assert _codes(docs) == ["N935", "U116"]
    assert flags == []


def test_non_eu_line_has_no_u116():
    docs, flags = tr.resolve_line_docs(
        code8="07049010", is_eu_origin=False, invoice_number="INV-3")
    assert _codes(docs) == ["N935", "Y929"]


def test_missing_rex_leaves_u116_id_blank():
    docs, _ = tr.resolve_line_docs(
        code8="07049010", is_eu_origin=True, invoice_number="INV-4", rex_ref="")
    u116 = [d for d in docs if d["code"] == "U116"][0]
    assert u116["id"] == ""   # gap surfaces to the reviewer — never guessed


def test_animal_code_without_n853_in_list_is_flagged_not_added():
    docs, flags = tr.resolve_line_docs(
        code8="04061030", is_eu_origin=True,
        invoice_number="INV-5", rex_ref="ITREXIT06167560157")
    assert "N853" not in _codes(docs)          # never auto-added
    assert len(flags) == 1 and "N853" in flags[0]


def test_animal_code_with_n853_in_list_is_not_flagged():
    docs, flags = tr.resolve_line_docs(
        code8="04061030", is_eu_origin=True,
        invoice_number="INV-6", rex_ref="ITREXIT06167560157",
        list_docs=[{"code": "N853", "id": "GBCHD2026.1234567",
                    "status": "AE", "reason": ""}])
    assert _codes(docs) == ["N935", "Y929", "U116", "N853"]
    assert flags == []


def test_list_docs_follow_and_overflow_is_callers_contract():
    # EU food line + one list doc → 4 docs; the caller writes docs[:3] and
    # must flag docs[3:]. The engine's contract: complete + ordered.
    docs, _ = tr.resolve_line_docs(
        code8="04061030", is_eu_origin=True,
        invoice_number="INV-7", rex_ref="ITREXIT06167560157",
        list_docs=[{"code": "N853", "id": "X", "status": "AE", "reason": ""}])
    assert len(docs) == 4
    assert [d["code"] for d in docs[3:]] == ["N853"]


def test_list_docs_input_not_mutated():
    src = [{"code": "N853", "id": "X", "status": "AE", "reason": ""}]
    tr.resolve_line_docs(code8="04061030", is_eu_origin=True, list_docs=src)
    assert len(src) == 1  # engine copies, never mutates caller state
