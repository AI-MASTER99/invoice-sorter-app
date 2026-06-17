"""Unit tests for review.py — the pure issue detector.

No app, no DB, no network: we feed hand-built dicts shaped exactly like what
the pipeline produces (compare_totals, find_cell_disagreements, rows_match) and
assert the detector returns clear, located messages with the right numbers.

Run:  python -m pytest invoiceflow/tests_review.py -q
"""
import review


def _tc(reported_computed):
    """Build a totals_check dict like compare_totals() returns."""
    out = {}
    for key, (rep, comp) in reported_computed.items():
        out[key] = {"reported": rep, "computed": comp, "match": None}
    return out


def _row(code="0702001099", desc="TOMATOES", **extra):
    row = {
        "Invoice": "SE 2692",
        "Comm./imp. cod": code,
        "Description of Goods": desc,
        "Origin": "IT",
        "Country": "Italy",
        "Number of Packages": "1",
        "Gross Weight (KG)": "8.00",
        "Net Weight (KG)": "7.70",
        "Value": "€46.20",
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------
# Clean invoice — nothing wrong
# --------------------------------------------------------------------------
def test_clean_invoice_has_no_high_or_medium():
    totals_check = _tc({
        "total_packages": ("78", "78"),
        "total_gross_kg": ("380.70", "380.70"),
        "total_net_kg": ("358.07", "358.07"),
        "total_value": ("2449.84", "2449.84"),
    })
    issues = review.build_review_issues(
        rows=[_row(), _row(code="0406103090", desc="MOZZARELLA CHEESE")],
        totals_check=totals_check,
        ab_reasons=[],
        flagged_cells=[set(), set()],
    )
    assert issues == []
    summary = review.summarize(issues)
    assert summary["status"] == "verified"
    assert summary["total"] == 0


# --------------------------------------------------------------------------
# Totals — strict, per field, with clear numbers
# --------------------------------------------------------------------------
def test_net_weight_mismatch_is_high_with_exact_numbers():
    totals_check = _tc({
        "total_packages": ("78", "78"),
        "total_gross_kg": ("380.70", "380.70"),
        "total_net_kg": ("358.07", "354.57"),   # 3.5 kg short
        "total_value": ("2449.84", "2449.84"),
    })
    issues = review.build_review_issues(rows=[_row()], totals_check=totals_check)
    nets = [i for i in issues if i["field"] == "net weight (kg)"]
    assert len(nets) == 1
    iss = nets[0]
    assert iss["severity"] == "high"
    assert iss["location"] == "Totals"
    assert iss["expected"] == "358.07 kg"
    assert iss["found"] == "354.57 kg"
    assert "-3.50 kg" in iss["message"]
    assert review.summarize(issues)["status"] == "needs_review"


def test_total_value_mismatch_is_flagged():
    totals_check = _tc({"total_value": ("2449.84", "2450.00")})
    issues = review.check_totals(totals_check)
    assert len(issues) == 1
    assert issues[0]["severity"] == "high"
    assert issues[0]["field"] == "total value"


def test_packages_shown_without_decimals():
    totals_check = _tc({"total_packages": ("78", "79")})
    iss = review.check_totals(totals_check)[0]
    assert iss["expected"] == "78"
    assert iss["found"] == "79"
    assert "+1" in iss["message"]


def test_each_total_judged_independently():
    totals_check = _tc({
        "total_packages": ("78", "78"),        # ok
        "total_gross_kg": ("380.70", "400.00"),  # wrong
        "total_net_kg": ("358.07", "358.07"),   # ok
        "total_value": ("2449.84", "2449.84"),  # ok
    })
    issues = review.check_totals(totals_check)
    assert len(issues) == 1
    assert issues[0]["field"] == "gross weight (kg)"


# --------------------------------------------------------------------------
# Float rounding is absorbed; a real 1-cent difference is not
# --------------------------------------------------------------------------
def test_float_rounding_is_absorbed():
    totals_check = _tc({"total_value": ("100.00", "100.004")})
    assert review.check_totals(totals_check) == []


def test_one_cent_difference_is_an_error():
    totals_check = _tc({"total_value": ("100.00", "100.01")})
    assert len(review.check_totals(totals_check)) == 1


def test_missing_invoice_total_is_info_not_high():
    totals_check = _tc({"total_net_kg": ("", "354.57")})
    issues = review.check_totals(totals_check)
    assert len(issues) == 1
    assert issues[0]["severity"] == "info"


# --------------------------------------------------------------------------
# Not-in-list rows
# --------------------------------------------------------------------------
def test_not_in_list_via_marker():
    rows = [_row(code="07049010",
                 desc=review.NOT_IN_LIST_MARKER + " CIMA RAPA")]
    issues = review.check_rows(rows, flagged_cells=[set()])
    assert len(issues) == 1
    iss = issues[0]
    assert iss["severity"] == "high"
    assert iss["location"] == "Line 1"
    assert "CIMA RAPA" in iss["message"]
    assert "07049010" in iss["message"]


def test_not_in_list_via_flag_field():
    rows = [_row(code="07032000", desc="AGLIO", _not_in_list=True)]
    issues = review.check_rows(rows)
    assert len(issues) == 1 and issues[0]["severity"] == "high"


def test_not_in_list_row_does_not_also_raise_uncertain():
    rows = [_row(desc=review.NOT_IN_LIST_MARKER + " X")]
    issues = review.check_rows(rows, flagged_cells=[{"Value", "Net Weight (KG)"}])
    assert len(issues) == 1  # only the not-in-list one, no uncertain noise


# --------------------------------------------------------------------------
# Uncertain cells (A/B disagreement)
# --------------------------------------------------------------------------
def test_uncertain_cell_is_medium_and_lists_fields():
    rows = [_row()]
    issues = review.check_rows(rows, flagged_cells=[{"Net Weight (KG)", "Value"}])
    assert len(issues) == 1
    iss = issues[0]
    assert iss["severity"] == "medium"
    assert "net weight" in iss["field"]
    assert "value" in iss["field"]


# --------------------------------------------------------------------------
# Structural (row count differs)
# --------------------------------------------------------------------------
def test_row_count_difference_is_high():
    issues = review.check_structure(
        ["Row count differs: Run A has 44 rows, Run B has 43 rows"])
    assert len(issues) == 1
    assert issues[0]["severity"] == "high"
    assert issues[0]["location"] == "Whole invoice"


# --------------------------------------------------------------------------
# Ordering — high issues come before medium/info
# --------------------------------------------------------------------------
def test_issues_sorted_high_first():
    totals_check = _tc({"total_value": ("100.00", "105.00")})  # high
    rows = [_row(), _row()]
    flagged = [{"Value"}, set()]  # one medium
    issues = review.build_review_issues(
        rows=rows, totals_check=totals_check, flagged_cells=flagged)
    severities = [i["severity"] for i in issues]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "info": 2}[s])
    assert severities[0] == "high"


# --------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------
def test_currency_mixed_is_high():
    rows = [_row(), _row()]
    rows[1]["Value"] = "£5.00"
    issues = review.check_currency(rows, {})
    assert len(issues) == 1 and issues[0]["severity"] == "high"
    assert "more than one currency" in issues[0]["message"]


def test_currency_mismatch_with_total():
    rows = [_row()]  # € lines
    issues = review.check_currency(rows, {"total_value_raw": "£46.20"})
    assert len(issues) == 1 and issues[0]["severity"] == "high"
    assert issues[0]["expected"] == "GBP" and issues[0]["found"] == "EUR"


def test_currency_symbol_and_code_are_equal():
    # Lines in '€', total written as 'EUR' — same currency, no issue.
    rows = [_row(), _row()]  # Value '€46.20'
    assert review.check_currency(rows, {"total_value_raw": "EUR 2.449,84"}) == []


def test_currency_none_is_info():
    rows = [_row()]
    rows[0]["Value"] = "46.20"  # no symbol
    issues = review.check_currency(rows, {})
    assert len(issues) == 1 and issues[0]["severity"] == "info"


def test_currency_clean():
    assert review.check_currency([_row(), _row()], {"total_value_raw": "€100"}) == []


# --------------------------------------------------------------------------
# Weights sanity
# --------------------------------------------------------------------------
def test_net_exceeds_gross():
    r = _row()
    r["Gross Weight (KG)"] = "5"
    r["Net Weight (KG)"] = "8"
    issues = review.check_weights([r])
    over = [i for i in issues if "impossible" in i["message"]]
    assert len(over) == 1 and over[0]["severity"] == "high"


def test_missing_gross_and_net():
    r = _row()
    r["Gross Weight (KG)"] = ""
    r["Net Weight (KG)"] = "0"
    issues = review.check_weights([r])
    fields = {i["field"] for i in issues}
    assert "gross weight" in fields and "net weight" in fields


def test_fee_row_skips_weight_checks():
    fee = _row(desc="TRASPORTO", code="")
    fee["Gross Weight (KG)"] = ""
    fee["Net Weight (KG)"] = ""
    assert review.check_weights([fee]) == []


def test_weight_lines_grouped():
    rows = [_row(), _row(), _row()]
    rows[0]["Net Weight (KG)"] = "99"   # net > gross on line 1
    rows[2]["Net Weight (KG)"] = "99"   # net > gross on line 3
    issues = review.check_weights(rows)
    over = [i for i in issues if "impossible" in i["message"]][0]
    assert "lines 1 and 3" in over["message"]


# --------------------------------------------------------------------------
# Fields: origin, code, value, empty rows
# --------------------------------------------------------------------------
def test_missing_origin():
    r = _row()
    r["Origin"] = ""
    issues = review.check_fields([r])
    assert any(i["field"] == "origin" and "missing" in i["message"] for i in issues)


def test_invalid_origin_code():
    r = _row()
    r["Origin"] = "ZZ"
    issues = review.check_fields([r])
    assert any(i["field"] == "origin" and "not a valid" in i["message"] for i in issues)


def test_missing_commodity_code():
    r = _row(code="")
    issues = review.check_fields([r])
    assert any(i["field"] == "commodity code" and "missing" in i["message"] for i in issues)


def test_bad_commodity_code_length():
    r = _row(code="123")
    issues = review.check_fields([r])
    assert any(i["field"] == "commodity code" and "length" in i["message"] for i in issues)


def test_fee_row_skips_field_checks():
    fee = _row(desc="CONTRIBUTO CONAI", code="", )
    fee["Origin"] = ""
    assert review.check_fields([fee]) == []


def test_not_in_list_row_skips_code_check():
    r = _row(code="07049010", desc=review.NOT_IN_LIST_MARKER + " CIMA RAPA")
    issues = review.check_fields([r])
    # 8-digit code is fine; not-in-list code check is skipped, origin valid
    assert all(i["field"] != "commodity code" for i in issues)


def test_empty_row_flagged():
    empty = {"Comm./imp. cod": "", "Description of Goods": "", "Value": "",
             "Origin": "", "Gross Weight (KG)": "", "Net Weight (KG)": ""}
    issues = review.check_fields([empty])
    assert any(i["field"] == "empty row" for i in issues)


def test_clean_goods_row_no_field_issues():
    assert review.check_fields([_row(), _row()]) == []


# --------------------------------------------------------------------------
# review_payload — maps a stored invoice record to {summary, issues}
# --------------------------------------------------------------------------
def test_review_payload_clean_invoice():
    invoice = {
        "rows": [_row()],
        "totals": {},
        "totals_check": _tc({
            "total_gross_kg": ("8.00", "8.00"),
            "total_net_kg": ("7.70", "7.70"),
            "total_value": ("46.20", "46.20"),
        }),
        "ab_reasons": [],
    }
    payload = review.review_payload(invoice)
    assert payload["summary"]["status"] == "verified"
    assert payload["issues"] == []


def test_review_payload_with_problems():
    invoice = {
        "rows": [_row(code="07049010",
                      desc=review.NOT_IN_LIST_MARKER + " CIMA RAPA")],
        "totals_check": _tc({"total_net_kg": ("358.07", "354.57")}),
        "ab_reasons": [],
    }
    payload = review.review_payload(invoice)
    assert payload["summary"]["status"] == "needs_review"
    assert payload["summary"]["high"] == 2  # net mismatch + not-in-list
    fields = {i["field"] for i in payload["issues"]}
    assert "net weight (kg)" in fields
    assert "commodity code" in fields


def test_review_payload_tolerates_missing_and_none():
    assert review.review_payload(None)["issues"] == []
    assert review.review_payload({})["summary"]["status"] == "verified"
    # Old invoice with only rows, no totals_check / ab_reasons
    payload = review.review_payload({"rows": [_row()]})
    assert payload["issues"] == []
