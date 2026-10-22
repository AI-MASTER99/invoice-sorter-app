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


# ── what the adversarial review of the first cut turned up ──────────────
def _with_dimension(raw: bytes, ref: str) -> bytes:
    """The same workbook with its sheet <dimension> rewritten.

    A streaming writer declares a dimension it has not filled in yet;
    openpyxl's read_only mode believes it.
    """
    import re
    import zipfile
    zin = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("sheet1.xml"):
                data = re.sub(rb'<dimension ref="[^"]*"\s*/>',
                              f'<dimension ref="{ref}"/>'.encode(), data)
            zout.writestr(item, data)
    return out.getvalue()


def test_an_understated_sheet_dimension_does_not_hide_goods_lines():
    """read_only mode trusts the sheet's own <dimension>. A workbook that
    understates it would render the header and nothing else — every goods
    line gone, and the render still long enough to look readable."""
    raw = _book([["Item", "Code"], ["AECAN", "7310219100"],
                 ["CLIP", "2903510090"], ["NUT", "7318163990"]])
    text = m.extract_spreadsheet_text(_with_dimension(raw, "A1:B1"), XLSX)
    assert "7310219100" in text and "2903510090" in text and "7318163990" in text


def test_a_newline_in_a_cell_does_not_split_the_row():
    """Alt+Enter in a description used to end the line there, so every
    value after it read one column to the left — the commodity code
    landing in the net-weight column."""
    text = m.extract_spreadsheet_text(_book([
        ["Item", "Description", "Net weight", "Code", "Net amount"],
        [1757065, "AECAN 65X195\nNI18IC L4PS WHITE", 86.4, "7310219100", 453.6],
    ]), XLSX)
    body = [l for l in text.splitlines() if not l.startswith("---")]
    assert len(body) == 2                      # header row + one goods row
    cells = [c.strip() for c in body[1].split("|")]
    assert cells == ["1757065", "AECAN 65X195 NI18IC L4PS WHITE",
                     "86.4", "7310219100", "453.6"]


def test_a_formula_with_no_cached_value_is_marked_not_blanked():
    """openpyxl and Apache POI write formulas with no cached result. The
    value pass returns None for those, which emptied a money column — and
    dropped a row that was nothing but formulas, losing a goods line."""
    wb = openpyxl.Workbook()
    wb.active.append(["Item", "Qty", "Price", "Net"])
    wb.active.append([1757065, 1080, 0.42, "=B2*C2"])
    wb.active.append(["=A2", "=B2", "=C2", "=D2"])     # all-formula row
    buf = io.BytesIO()
    wb.save(buf)
    text = m.extract_spreadsheet_text(buf.getvalue(), XLSX)
    body = [l for l in text.splitlines() if not l.startswith("---")]
    assert len(body) == 3                      # the all-formula row survives
    assert m._UNCALCULATED in body[1]          # the net amount is marked
    assert body[2].count(m._UNCALCULATED) == 4


def test_a_cached_formula_still_renders_its_value():
    """The real supplier workbook totals with SUM and Excel caches the
    result; marking those would be a false alarm on every invoice."""
    real = Path("/tmp/claude-0/-home-user-invoice-sorter-app"
                "/e14e4b84-3e0d-5834-b096-723483bd6595/scratchpad/guy_zip"
                "/LD00052556 UK DGM.XLSX")
    if not real.exists():
        pytest.skip("supplier workbook not present in this checkout")
    text = m.extract_spreadsheet_text(real.read_bytes(), XLSX)
    assert m._UNCALCULATED not in text
    assert "74289.64" in text and "74289.64000000001" not in text


def test_a_binary_float_does_not_print_its_own_error():
    """The real invoice total came back as 74289.64000000001 where every
    document in the pack says 74,289.64. Driven through _sheet_cell_text
    directly: round-tripping the value through openpyxl's writer would
    normalise it before the renderer ever saw it."""
    assert m._sheet_cell_text(74289.64000000001) == "74289.64"
    assert m._sheet_cell_text(13102.579999999998) == "13102.58"
    assert m._sheet_cell_text(86.4) == "86.4"           # untouched
    assert m._sheet_cell_text(1977.56) == "1977.56"     # untouched


def test_a_leading_zero_code_keeps_its_ten_digits():
    """Chapters 01-09 are stored as the integer and DISPLAYED with the
    zero. Dropping it blinds review.codes_in_text, which is the one check
    that notices a goods line the model never returned."""
    import review
    wb = openpyxl.Workbook()
    wb.active.append(["Code", "Description"])
    wb.active.append([406103000, "CHEESE"])
    wb.active.cell(2, 1).number_format = "0000000000"
    buf = io.BytesIO()
    wb.save(buf)
    text = m.extract_spreadsheet_text(buf.getvalue(), XLSX)
    assert "0406103000" in text
    assert review.codes_in_text(text) == ["0406103000"]


def test_an_ordinary_number_is_not_zero_padded():
    text = m.extract_spreadsheet_text(_book([["Qty"], [1080]]), XLSX)
    assert "1080" in text and "0001080" not in text


def test_the_render_is_capped_in_characters_not_only_rows():
    """Rows and columns alone are not a memory bound: a small workbook can
    declare a million populated cells on one shared 512 MB worker."""
    big = "X" * 500
    text = m.extract_spreadsheet_text(
        _book([[big] * 60 for _ in range(300)]), XLSX)
    assert len(text) <= m._RENDER_MAX_CHARS + 10_000
    assert "TRUNCATED" in text


def test_one_absurd_cell_is_clipped():
    text = m.extract_spreadsheet_text(_book([["Note"], ["y" * 50_000]]), XLSX)
    assert "y" * m._CELL_MAX_CHARS in text
    assert "y" * (m._CELL_MAX_CHARS + 1) not in text


def test_an_empty_template_is_refused_on_its_rows_not_its_headers():
    """The sheet header this renderer writes is ~75 characters, so a
    multi-tab template with a few stray characters in it used to clear the
    100-character readability bar on the headers alone, and the job spent
    four model calls discovering there was nothing there."""
    wb = openpyxl.Workbook()
    wb.active.title = "Goods lines"              # the tab that should hold them
    wb.create_sheet("Notes").append(["x"])
    wb.create_sheet("Instructions").append(["y"])
    buf = io.BytesIO()
    wb.save(buf)
    text = m.extract_spreadsheet_text(buf.getvalue(), XLSX)
    assert len(text) > 100                       # the headers alone clear it
    assert m.spreadsheet_body_chars(text) == 2   # the row content is "x", "y"
    with pytest.raises(m.UnreadableUpload):
        m._require_spreadsheet_text(buf.getvalue(), XLSX)


def test_a_small_but_real_invoice_sheet_is_still_accepted():
    """The guard above must not refuse a short genuine invoice."""
    text = m.extract_spreadsheet_text(_book([
        ["Item", "Description", "Qty", "Unit price", "Net amount", "Code"],
        [1757065, "AECAN 65X195 NI18IC L4PS WHITE", 1080, 0.42, 453.6,
         "7310219100"],
    ]), XLSX)
    assert "7310219100" in text
