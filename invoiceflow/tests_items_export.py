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


def test_a_list_national_additional_code_overrides_the_default():
    """How a chapter-22 wine line, or an X570 product, is corrected."""
    wine = _row(code="22042166")
    wine["_cds"] = {"nat_add_code": "", "documents": []}
    ws, col = _items([wine])
    assert _cell(ws, col, 4, _NAC).value == "VATZ"   # chapter test alone

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
