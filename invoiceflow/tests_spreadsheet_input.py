"""Reading an invoice that arrives as a spreadsheet instead of a PDF.

The reason this path exists is a goods line that went missing on a real
entry. The supplier sends its invoice as a workbook; the forwarder saved
it as a PDF; the description column was too narrow, so in the PDF's text
layer "NICKEL CLIPS WHITE" and the net weight 86.40 beside it came out as
one run of characters. The line was never extracted and the declaration
under-declared the import. A workbook has its cells already apart, so
reading it directly cannot fail that way.

Imports main, so it relies on a valid .env (dummy values are fine).
Run: cd invoiceflow && python -m pytest tests_spreadsheet_input.py -q
"""
import io
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import main as m  # noqa: E402 — relies on .env being valid

XLSX = m.MIME_MAP[".xlsx"]
CSV = m.MIME_MAP[".csv"]


def _book(rows, title="Sheet1", extra=None):
    """Rows (and optionally a second sheet) as .xlsx bytes."""
    wb = openpyxl.Workbook()
    wb.active.title = title
    for r in rows:
        wb.active.append(r)
    if extra:
        ws2 = wb.create_sheet(extra[0])
        for r in extra[1]:
            ws2.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── what the format change is for ───────────────────────────────────────
def test_a_description_cannot_run_into_the_column_beside_it():
    """The real failure, as a test: in the printed PDF the description and
    the net weight came out as 'NI8168.I4C0 L4PS WHITE86.40'. From the
    workbook they are separate cells and stay separate."""
    text = m.extract_spreadsheet_text(_book([
        ["Item", "Description", "Net weight", "Commodity code", "Net amount"],
        [1757065, "AECAN 65X195 NI18IC L4PS WHITE", 86.40, "7310219100", 453.60],
    ]), XLSX)
    [line] = [l for l in text.splitlines() if "AECAN" in l]
    cells = [c.strip() for c in line.split("|")]
    assert "AECAN 65X195 NI18IC L4PS WHITE" in cells
    assert "86.4" in cells
    assert "WHITE86.40" not in line


def test_the_printed_codes_check_still_sees_the_codes():
    """review.codes_in_text reads the same text the PDF path produces, so
    the missing-line warning keeps working on a spreadsheet upload."""
    import review
    text = m.extract_spreadsheet_text(_book([
        ["Description", "Commodity code"],
        ["DRY COOLANT SPRAY", "2903510090"],
        ["EMPTY STEEL AEROSOL CANISTERS", "7310219100"],
    ]), XLSX)
    assert sorted(review.codes_in_text(text)) == ["2903510090", "7310219100"]


# ── cell rendering ──────────────────────────────────────────────────────
def test_a_code_held_as_a_number_keeps_its_digits():
    """openpyxl returns 2922498590.0 for a code typed as a number. A
    decimal point inside a commodity code would be a wrong declaration."""
    text = m.extract_spreadsheet_text(_book([
        ["Code", "Qty"], [2922498590, 1080],
    ]), XLSX)
    assert "2922498590 | 1080" in text
    assert "2922498590.0" not in text


def test_a_real_decimal_is_left_alone():
    text = m.extract_spreadsheet_text(_book([["Net"], [86.4]]), XLSX)
    assert "86.4" in text


def test_dates_booleans_and_blanks():
    text = m.extract_spreadsheet_text(_book([
        ["Date", "Flag", "Empty", "Note"],
        [datetime(2026, 10, 19, 11, 30), True, None, "  padded  "],
    ]), XLSX)
    row = text.splitlines()[-1]
    assert row == "2026-10-19 | TRUE |  | padded"


def test_blank_rows_and_trailing_blank_cells_are_dropped():
    text = m.extract_spreadsheet_text(_book([
        ["A", "B", "C"], [None, None, None], ["x", None, None],
    ]), XLSX)
    body = [l for l in text.splitlines() if not l.startswith("---")]
    assert body == ["A | B | C", "x"]


# ── sheets and CSV ──────────────────────────────────────────────────────
def test_every_sheet_is_rendered_and_named():
    text = m.extract_spreadsheet_text(
        _book([["A"], ["one"]], title="Lines",
              extra=("Totals", [["Total"], [42]])), XLSX)
    assert "--- SHEET: Lines" in text and "--- SHEET: Totals" in text
    assert "one" in text and "42" in text


@pytest.mark.parametrize("raw", [
    b"Code,Qty\r\n2903510090,12\r\n",
    b"Code;Qty\r\n2903510090;12\r\n",          # continental delimiter
    b"\xef\xbb\xbfCode,Qty\r\n2903510090,12\r\n",   # BOM
])
def test_csv_is_read_whatever_the_delimiter(raw):
    text = m.extract_spreadsheet_text(raw, CSV)
    assert "Code | Qty" in text
    assert "2903510090 | 12" in text


# ── routing and failure ─────────────────────────────────────────────────
def test_the_upload_types_are_accepted():
    for ext in (".xlsx", ".xlsm", ".csv"):
        assert ext in m.MIME_MAP
        assert m.is_spreadsheet(m.MIME_MAP[ext])
    # .xls is deliberately absent: openpyxl cannot read the binary format,
    # so accepting it would fail after the upload rather than at it.
    assert ".xls" not in m.MIME_MAP
    for ext in (".pdf", ".png", ".docx"):
        assert not m.is_spreadsheet(m.MIME_MAP[ext])


def test_document_text_routes_a_workbook_to_the_spreadsheet_reader():
    book = _book([["Code"], ["2903510090"]])
    assert m._document_text(book, XLSX) == m.extract_spreadsheet_text(book, XLSX)
    assert m._document_text(b"not a pdf", "image/png") == ""


def test_a_file_that_is_not_a_workbook_says_so():
    """A workbook has no vision fallback, so it must fail with something
    the operator can act on rather than be sent on labelled as a PDF."""
    with pytest.raises(m.UnreadableUpload):
        m.extract_spreadsheet_text(b"%PDF-1.4 not a workbook", XLSX)


def test_an_empty_workbook_is_refused_not_sent_on():
    with pytest.raises(m.UnreadableUpload):
        m._require_spreadsheet_text(_book([]), XLSX)


def test_the_refusal_reaches_the_operator_unchanged():
    msg = "That spreadsheet has no rows to read."
    assert m._user_facing_job_error(m.UnreadableUpload(msg)) == msg


def test_a_runaway_sheet_is_truncated_and_says_so():
    text = m.extract_spreadsheet_text(
        _book([[f"row {i}"] for i in range(m._SHEET_MAX_ROWS + 50)]), XLSX)
    assert "TRUNCATED" in text
    body = [l for l in text.splitlines() if not l.startswith("---")]
    assert len(body) == m._SHEET_MAX_ROWS
