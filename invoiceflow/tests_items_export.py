"""MultiFreight Items export — the DE 2/1 previous-document columns.

Every goods line the export writes refers back to the commercial invoice
it was declared from: category Z, type 380, reference = the invoice
number as printed on the invoice (never a customer code or order number).
The invoice number is the one the extraction step read off the invoice,
so a wrong reading shows up in this column — the reference is left blank
rather than guessed when no number is known.

Imports main, so it relies on a valid .env (dummy values are fine).
Run: cd invoiceflow && python -m pytest tests_items_export.py -q
"""
import io
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).parent))
import main as m  # noqa: E402 — relies on .env being valid


def _row(code="20060035", invoice="0002236", **over):
    row = {"Invoice": invoice, "Comm./imp. cod": code,
           "Description of Goods": "GOODS", "Origin": "IT", "Country": "Italy",
           "Number of Packages": "10", "Gross Weight (KG)": "120.5",
           "Net Weight (KG)": "110.25", "Value": "€500.00"}
    row.update(over)
    return row


def _items(rows, totals=None):
    data = m.build_items_xlsx(rows, totals=totals)
    ws = openpyxl.load_workbook(io.BytesIO(data))["Items"]
    col = {str(ws.cell(row=3, column=c).value).strip(): c
           for c in range(1, ws.max_column + 1)
           if ws.cell(row=3, column=c).value not in (None, "")}
    return ws, col


def _cell(ws, col, r, header):
    return ws.cell(row=r, column=col[header])


def test_every_line_refers_back_to_the_invoice_as_z_380():
    ws, col = _items([_row("20060035"), _row("08042090")])
    for r in (4, 5):
        assert _cell(ws, col, r, "[2/1] Previous Documents - Category (01)").value == "Z"
        assert _cell(ws, col, r, "[2/1] Previous Documents - Type (01)").value == "380"
        assert _cell(ws, col, r, "[2/1] Previous Documents - Ref (01)").value == "0002236"


def test_previous_document_cells_are_text_so_leading_zeros_survive():
    ws, col = _items([_row()])
    for h in ("[2/1] Previous Documents - Category (01)",
              "[2/1] Previous Documents - Type (01)",
              "[2/1] Previous Documents - Ref (01)"):
        assert _cell(ws, col, 4, h).number_format == "@"


def test_the_reference_is_the_invoice_number_not_anything_else():
    # The row's Invoice column is what the extraction read as the invoice
    # number; nothing else on the row (code, origin, REX) may leak into it.
    ws, col = _items([_row(invoice="FT-2026/118")],
                     totals={"supplier_rex": "ITREXIT06678581213"})
    assert _cell(ws, col, 4, "[2/1] Previous Documents - Ref (01)").value == "FT-2026/118"
    # And the N935 invoice document carries the same number.
    assert _cell(ws, col, 4, "[2/3] Documents - ID (01)").value == "VM1 FT-2026/118"


def test_an_unknown_invoice_number_leaves_the_reference_blank_not_guessed():
    ws, col = _items([_row(invoice="")])
    assert _cell(ws, col, 4, "[2/1] Previous Documents - Category (01)").value == "Z"
    assert _cell(ws, col, 4, "[2/1] Previous Documents - Type (01)").value == "380"
    assert _cell(ws, col, 4, "[2/1] Previous Documents - Ref (01)").value in (None, "")


def test_only_the_first_previous_document_slot_is_used():
    ws, col = _items([_row()])
    for h in ("[2/1] Previous Documents - Category (02)",
              "[2/1] Previous Documents - Type (02)",
              "[2/1] Previous Documents - Ref (02)",
              "[2/1] Previous Documents - Line (01)"):
        assert _cell(ws, col, 4, h).value in (None, "")


def test_a_fee_row_is_not_an_items_line():
    ws, col = _items([_row(), _row(code="", **{"Description of Goods": "TRANSPORT"})])
    assert _cell(ws, col, 5, "[2/1] Previous Documents - Category (01)").value in (None, "")


# ── [6/17] National Additional Codes — VATZ is a food relief ────────────
_NAC = "[6/17] National Additional Codes - Code (01)"


def test_vatz_goes_on_a_food_line():
    ws, col = _items([_row(code="19059080")])        # baked pastry, ch. 19
    assert _cell(ws, col, 4, _NAC).value == "VATZ"


def test_vatz_stays_off_a_non_food_line():
    # Was "VATZ on every item"; the bulk of the list is not food.
    ws, col = _items([_row(code="76109010")])        # aluminium ladder, ch. 76
    assert _cell(ws, col, 4, _NAC).value in (None, "")


def test_each_line_is_judged_on_its_own_code():
    ws, col = _items([_row(code="19059080"), _row(code="84282000")])
    assert _cell(ws, col, 4, _NAC).value == "VATZ"
    assert _cell(ws, col, 5, _NAC).value in (None, "")


def test_chapter_22_gets_no_vatz_by_default():
    """Drinks are out of VATZ even though ch. 22 sits inside 01-24."""
    wine = _row(code="22042166")
    wine["_cds"] = {"nat_add_code": "", "documents": []}
    ws, col = _items([wine])
    assert _cell(ws, col, 4, _NAC).value in (None, "")


def test_a_list_national_additional_code_overrides_the_default():
    """How an X570 product is corrected. Note the list can only SUBSTITUTE
    a value here: a blank cell means "use the VATZ default"."""
    grease = _row(code="27101999")
    grease["_cds"] = {"nat_add_code": "X570", "documents": []}
    ws, col = _items([grease])
    assert _cell(ws, col, 4, _NAC).value == "X570"   # list wins


# ── Documents carried by the commodity-code list ────────────────────────
def test_a_list_document_is_declared_after_the_automatic_ones():
    """CRC's DRY COOLANT SPRAY: Y926 comes from the list, not the engine."""
    row = _row(code="29035100", invoice="000126985")
    row["_cds"] = {"documents": [{"code": "Y926",
                                  "id": "EXCLUDED FROM PROHIBITION",
                                  "status": "",
                                  "reason": "EXCLUDED FROM PROHIBITION"}]}
    ws, col = _items([row], totals={"supplier_rex": "BEREX123"})
    codes = [_cell(ws, col, 4, f"[2/3] Documents - Code (0{i})").value
             for i in (1, 2, 3)]
    assert codes == ["N935", "U116", "Y926"]        # engine's two, then the list's
    assert _cell(ws, col, 4, "[2/3] Documents - ID (03)").value == \
        "EXCLUDED FROM PROHIBITION"
    assert _cell(ws, col, 4, "[2/3] Documents - Status (03)").value in (None, "")


def test_a_line_with_no_list_documents_is_unchanged():
    ws, col = _items([_row(code="29035100")])
    assert _cell(ws, col, 4, "[2/3] Documents - Code (01)").value == "N935"
    assert _cell(ws, col, 4, "[2/3] Documents - Code (03)").value in (None, "")


def test_the_worst_real_line_fits_the_six_slots():
    """Food + EU + two list documents = 5 of 6; nothing is dropped."""
    row = _row(code="19059080", invoice="INV-1")
    row["_cds"] = {"documents": [
        {"code": "Y937", "id": "REG 2019/1793 EXEMPT", "status": "",
         "reason": "REG 2019/1793 EXEMPT"},
        {"code": "Y930", "id": "2007/275/EC EXEMPT", "status": "",
         "reason": "2007/275/EC EXEMPT"}]}
    ws, col = _items([row], totals={"supplier_rex": "FRREX1"})
    codes = [_cell(ws, col, 4, f"[2/3] Documents - Code (0{i})").value
             for i in range(1, 7)]
    assert codes[:5] == ["N935", "Y929", "U116", "Y937", "Y930"]
    assert codes[5] in (None, "")
    # No ">6 DOCS" warning on the description.
    assert "NOT DECLARED" not in (_cell(ws, col, 4, "[6/8] Description of Goods").value or "")


# ── Document slots 04-06 sit directly after slot 03 ─────────────────────
def test_the_six_document_slots_are_contiguous():
    """The operator reads the sheet by eye; 04-06 must follow 03.

    Allowed by the template's own FAQ ("Tips and Tricks"): column order is
    free, only the row-3 header text is the contract.
    """
    from openpyxl.utils import get_column_letter as L
    ws, col = _items([_row()])
    cols = [col[f"[2/3] Documents - Code (0{i})"] for i in range(1, 7)]
    assert [L(c) for c in cols] == ["BA", "BJ", "BS", "CB", "CK", "CT"]
    assert cols == sorted(cols) and all(b - a == 9 for a, b in zip(cols, cols[1:]))


def test_no_template_header_is_lost_or_renamed():
    """Renaming a row-3 header gets the whole sheet rejected on import."""
    import openpyxl
    tpl = openpyxl.load_workbook(m._ITEMS_TEMPLATE)["Items"]
    want = {str(tpl.cell(row=3, column=c).value).strip()
            for c in range(1, tpl.max_column + 1)
            if tpl.cell(row=3, column=c).value not in (None, "")}
    ws, col = _items([_row()])
    assert want - set(col) == set()


def test_the_new_slots_are_not_hidden():
    """They land where the template hid a band; the operator must see them."""
    from openpyxl.utils import get_column_letter as L
    ws, col = _items([_row()])
    first = col["[2/3] Documents - Code (04)"]
    last = col["[8/7] Documents - Write-off Unit (06)"]
    hidden = [L(c) for c in range(first, last + 1)
              if L(c) in ws.column_dimensions and ws.column_dimensions[L(c)].hidden]
    assert hidden == []


def test_shifted_hidden_bands_moved_with_their_columns():
    """insert_cols moves cells only — the width/hidden bands are hand-shifted."""
    ws, col = _items([_row()])
    bands = {d.min for d in ws.column_dimensions.values() if d.hidden}
    assert 115 in bands and 154 in bands      # template's 88 and 127, +27
    assert 88 not in bands and 127 not in bands


def test_the_sheet_keeps_its_layout_furniture():
    ws, col = _items([_row()])
    assert [str(r) for r in ws.merged_cells.ranges] == ["A1:M1"]
    assert ws.freeze_panes == "B1"
    # The 1M-row stray tail is pruned, so Excel reports a sane used range.
    assert ws.max_row < 200


def test_all_six_slots_are_written_and_the_seventh_still_warns():
    row = _row(code="04061030")
    row["_cds"] = {"documents": [
        {"code": c, "id": c, "status": "", "reason": c}
        for c in ("Y937", "C644", "Y900", "Y923")]}
    ws, col = _items([row], totals={"supplier_rex": "ITREX1"})
    codes = [_cell(ws, col, 4, f"[2/3] Documents - Code (0{i})").value
             for i in range(1, 7)]
    assert codes == ["N935", "Y929", "U116", "Y937", "C644", "Y900"]
    assert "NOT DECLARED: Y923" in _cell(ws, col, 4, "[6/8] Description of Goods").value


# ── A list wording must not turn a goods line into a "fee" ──────────────
def test_a_goods_line_survives_a_list_wording_that_reads_like_a_fee():
    """7310219100 is held as "METAL PACKAGING"; _is_fee_row matches that."""
    row = _row(code="7310219100", invoice="000126985")
    row["Description of Goods"] = "METAL PACKAGING"      # from the list
    row["_invoice_desc"] = "AECAN 65X195 CLIPS WHITE"    # from the invoice
    ws, col = _items([row])
    assert _cell(ws, col, 4, "[6/14] Commodity Code").value == "73102191"
    assert _cell(ws, col, 4, "[6/14] TARIC Code").value == "00"


def test_a_real_fee_line_is_still_left_out():
    fee = _row(code="", invoice="000126985")
    fee["Description of Goods"] = "TRANSPORT COSTS"
    ws, col = _items([fee])
    assert _cell(ws, col, 4, "[6/14] Commodity Code").value in (None, "")


def test_the_invoice_wording_decides_even_when_it_reads_like_a_fee():
    """The invoice calling it a charge still wins over a list wording."""
    row = _row(code="7310219100")
    row["Description of Goods"] = "EMPTY AEROSOL CANS"
    row["_invoice_desc"] = "PACKAGING SURCHARGE"
    ws, col = _items([row])
    assert _cell(ws, col, 4, "[6/14] Commodity Code").value in (None, "")
