"""Unit tests for cds_list — the CDS Items export -> V-lookup list parser.

Covers what the list depends on: the 8+2 commodity-code split (including
the dropped leading zero and the incomplete-code refusal), the REX that
attributes a line to a client, the canonical description (frequency,
casing, per-shipment references), and the fold into list entries.

Run: python -m pytest tests_cds_list.py -q
"""
from collections import Counter

import cds_list as cl

DOCS = "Documents (Code/ID/Part/Status/Reason/Licence(s)/Date/Qty/Unit)"


def row(code, desc, *, docs="", date="06/01/2026", origin="IT",
        pref="300", proc="4000"):
    return {
        "Acceptance Date": date,
        "Commodity Code": code,
        "Commodity Description": desc,
        "Origin Country": origin,
        "Preference": pref,
        "Procedure": proc,
        DOCS: docs,
    }


# ── Commodity code: 8 general digits + 2 TARIC ──────────────────────────
def test_split_code_splits_on_the_slash():
    assert cl.split_code("20059980/98") == ("20059980", "98")


def test_split_code_defaults_a_missing_taric_to_00():
    assert cl.split_code("84321000/") == ("84321000", "00")
    assert cl.split_code("84321000") == ("84321000", "00")


def test_split_code_restores_a_dropped_leading_zero():
    # 7 digits is the familiar chapter-01-09 zero drop (0210 1219 -> 2101219).
    assert cl.split_code("2101219/00") == ("02101219", "00")


def test_split_code_refuses_an_incomplete_code():
    # '852910/00' is in the 2026 export: a 6-digit stub, never a list entry.
    assert cl.split_code("852910/00") == ("", "")
    assert cl.split_code("") == ("", "")
    assert cl.split_code("GOODS CLEARED") == ("", "")


# ── REX: which client the line belongs to ───────────────────────────────
def test_rex_of_reads_the_u116_document():
    docs = ("N935/VM1 1845//JE///0.00/: U116/ITREXIT01207620012//JE///0.00/: "
            "Y929/EXCLUDED FROM REG 834/2007///Excluded from Reg 834/2007//0.00/")
    assert cl.rex_of({DOCS: docs}) == "ITREXIT01207620012"


def test_rex_of_is_empty_without_a_u116():
    assert cl.rex_of({DOCS: "N935/VM1 610//JE///0.00/"}) == ""
    assert cl.rex_of({}) == ""


# ── Description: one canonical wording per code ─────────────────────────
def test_choose_description_prefers_the_most_declared_wording():
    assert cl.choose_description(
        Counter({"OLIVES": 94, "ITALIAN OLIVES": 3, "GREEN OLIVES": 2})) == "OLIVES"


def test_choose_description_counts_casing_variants_together():
    # "Meat" + "MEAT" outweigh "BEEF"; the winner keeps its most-used casing.
    assert cl.choose_description(Counter({"MEAT": 2, "Meat": 1, "BEEF": 2})) == "MEAT"


def test_choose_description_is_deterministic_on_a_tie():
    picked = cl.choose_description(Counter({"TOMATOES": 3, "PEPPERS": 3}))
    assert picked == "PEPPERS"          # equal counts -> shortest, then A-Z
    assert cl.choose_description(Counter()) == ""


def test_clean_description_drops_per_shipment_references():
    assert (cl.clean_description("OCCITAN 50 MACHINE SERIAL NUMBER N158539")
            == "OCCITAN 50 MACHINE")
    assert (cl.clean_description("AVATAR 6 SL SERIAL NUMBER 21681379 MRN26DE8801885")
            == "AVATAR 6 SL")
    assert cl.clean_description("COMPRESSOR MRN 26DE300492958033B3") == "COMPRESSOR"


def test_clean_description_keeps_a_description_that_is_only_a_serial():
    # Nothing recognisable would be left — better a poor description than none.
    assert cl.clean_description("SERIAL NUMBER 12345") == "SERIAL NUMBER 12345"
    assert cl.clean_description("  GRILLED   PEPPERS ") == "GRILLED PEPPERS"


# ── The fold: export lines -> list entries ──────────────────────────────
def test_build_list_folds_lines_into_one_entry_per_code():
    u116 = "U116/ITREXIT06167560157//JE///0.00/"
    entries, stats = cl.build_list([
        row("07049010/00", "CABBAGE", docs=u116),
        row("07049010/00", "GREEN CABBAGE", docs=u116),
        row("07049010/00", "CABBAGE", docs=u116, date="21/04/2026", origin="GR"),
        row("852910/00", "ELEVATOR MATERIAL"),          # incomplete code
        row("", "GOODS CLEARED"),                       # admin line
    ])
    assert stats == {"rows": 5, "used": 3, "skipped_no_code": 2,
                     "entries": 1, "clients": 1}
    (entry,) = entries
    assert entry["rex"] == "ITREXIT06167560157"
    assert entry["general_code"] == "07049010"
    assert entry["full_code"] == "0704901000"
    assert entry["taric_code"] == "00"
    assert entry["description"] == "CABBAGE"            # 2 lines vs 1
    assert entry["lines"] == 3
    assert entry["last_used"] == "2026-04-21"           # latest acceptance date
    assert entry["origin"] == "GR"                      # from that latest line


def test_build_list_keeps_each_rex_separate():
    entries, stats = cl.build_list([
        row("07049010/00", "CABBAGE", docs="U116/ITREX1//JE///0.00/"),
        row("07049010/00", "SAVOY CABBAGE", docs="U116/BEREX2//JE///0.00/"),
        row("07049010/00", "CABBAGE"),                  # no REX (non-EU line)
    ])
    assert stats["entries"] == 3 and stats["clients"] == 2
    by_rex = cl.group_by_rex(entries)
    assert sorted(by_rex) == ["", "BEREX2", "ITREX1"]
    assert by_rex["BEREX2"][0]["description"] == "SAVOY CABBAGE"


def test_merge_by_code_collapses_rex_groups_for_one_client():
    entries = [
        {"full_code": "0704901000", "general_code": "07049010", "taric_code": "00",
         "description": "CABBAGE", "lines": 2, "rex": "ITREX1"},
        {"full_code": "0704901000", "general_code": "07049010", "taric_code": "00",
         "description": "SAVOY CABBAGE", "lines": 9, "rex": "BEREX2"},
        {"full_code": "2005700000", "general_code": "20057000", "taric_code": "00",
         "description": "OLIVES", "lines": 4, "rex": "ITREX1"},
    ]
    merged = cl.merge_by_code(entries)
    assert [e["full_code"] for e in merged] == ["0704901000", "2005700000"]
    assert merged[0]["description"] == "SAVOY CABBAGE"   # backed by more lines
    assert merged[0]["lines"] == 11                      # both groups counted


# ── Reading the export + the committed list ─────────────────────────────
EXPORT_CSV = (
    'Acceptance Date,Commodity Code,Commodity Description,Origin Country,'
    'Preference,Procedure,"' + DOCS + '"\r\n'
    '"06/01/2026","20059980/98","GRILLED PEPPERS caf\xe9","IT","300","4000",'
    '"N935/VM1 610//JE///0.00/: U116/ITREXIT06167560157//JE///0.00/"\r\n'
)


def test_read_export_handles_the_windows_encoding():
    rows = cl.read_export(EXPORT_CSV.encode("cp1252"))
    assert rows[0]["Commodity Description"] == "GRILLED PEPPERS caf\xe9"


def test_parse_export_end_to_end():
    entries, stats = cl.parse_export(EXPORT_CSV.encode("cp1252"))
    assert stats["used"] == 1
    assert entries[0]["full_code"] == "2005998098"
    assert entries[0]["rex"] == "ITREXIT06167560157"


def test_derived_round_trip(tmp_path):
    entries, _ = cl.parse_export(EXPORT_CSV.encode("cp1252"))
    path = tmp_path / "commodity_codes.csv"
    cl.write_derived(entries, path)
    back = cl.read_derived(path)
    assert back == [{**e, "lines": e["lines"]} for e in entries]


def test_product_payload_is_the_commodity_codes_columns():
    entry = {"general_code": "07049010", "full_code": "0704901000",
             "taric_code": "00", "description": "CABBAGE", "lines": 3,
             "rex": "ITREX1", "origin": "IT"}
    assert cl.product_payload(entry) == {
        "general_code": "07049010", "full_code": "0704901000",
        "taric_code": "00", "description": "CABBAGE",
    }


# ── A plain code list, merged in without duplicates ─────────────────────
LIST_CSV = (
    "Commodity Code,Additional Taric,Description\r\n"
    "02013000,90,TARTARE DI FASONA\r\n"
    "2101219,00,  ITALIAN   CURED MEAT \r\n"
    "852910,00,ELEVATOR MATERIAL\r\n"
    ",,\r\n"
)


def write_list(tmp_path, text=LIST_CSV, name="codes.csv"):
    path = tmp_path / name
    path.write_bytes(text.encode("cp1252"))
    return path


def test_read_code_list_reads_one_entry_per_row(tmp_path):
    entries, stats = cl.read_code_list(write_list(tmp_path))
    assert [e["full_code"] for e in entries] == ["0201300090", "0210121900"]
    assert entries[0]["description"] == "TARTARE DI FASONA"
    # 7-digit zero drop repaired, whitespace collapsed — same rules as the export.
    assert entries[1]["general_code"] == "02101219"
    assert entries[1]["description"] == "ITALIAN CURED MEAT"


def test_read_code_list_skips_an_incomplete_code(tmp_path):
    _, stats = cl.read_code_list(write_list(tmp_path))
    assert stats["rows"] == 3                    # the blank row is not a row
    assert stats["skipped_no_code"] == 1
    assert stats["skipped"] == [("852910", "00", "ELEVATOR MATERIAL")]


def test_read_code_list_carries_no_provenance(tmp_path):
    entries, _ = cl.read_code_list(write_list(tmp_path))
    # A code list says a code is used, not which declaration it came from.
    assert entries[0]["rex"] == "" and entries[0]["origin"] == ""
    assert entries[0]["lines"] == 0 and entries[0]["last_used"] == ""


def test_read_code_list_accepts_a_combined_code_column(tmp_path):
    path = write_list(tmp_path, "Code,Description\r\n20059980/98,PEPPERS\r\n")
    entries, _ = cl.read_code_list(path)
    assert entries[0]["full_code"] == "2005998098"


def test_read_code_list_folds_a_repeated_code(tmp_path):
    path = write_list(tmp_path, "Commodity Code,Additional Taric,Description\r\n"
                                "02013000,90,TARTARE\r\n02013000,90,BEEF\r\n")
    entries, _ = cl.read_code_list(path)
    assert [e["description"] for e in entries] == ["TARTARE"]


def test_read_code_list_needs_the_two_columns(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        cl.read_code_list(write_list(tmp_path, "Widget,Colour\r\na,b\r\n"))


def test_add_entries_adds_only_the_missing_codes():
    existing, _ = cl.parse_export(EXPORT_CSV.encode("cp1252"))   # 2005998098
    additions = [
        {"rex": "", "general_code": "20059980", "full_code": "2005998098",
         "taric_code": "98", "description": "SHEET WORDING", "lines": 0},
        {"rex": "", "general_code": "02013000", "full_code": "0201300090",
         "taric_code": "90", "description": "TARTARE DI FASONA", "lines": 0},
    ]
    merged, stats = cl.add_entries(existing, additions)
    assert (stats["added"], stats["already_present"]) == (1, 1)
    assert [e["full_code"] for e in merged] == ["0201300090", "2005998098"]
    # The code already declared keeps its export wording, not the sheet's.
    assert merged[1]["description"] == "GRILLED PEPPERS caf\xe9"


def test_add_entries_is_idempotent():
    existing, _ = cl.parse_export(EXPORT_CSV.encode("cp1252"))
    additions = [{"rex": "", "general_code": "02013000",
                  "full_code": "0201300090", "taric_code": "90",
                  "description": "TARTARE", "lines": 0}]
    once, _ = cl.add_entries(existing, additions)
    twice, stats = cl.add_entries(once, additions)
    assert stats == {"added": 0, "already_present": 1}
    assert twice == once


def test_add_entries_dedupes_against_every_rex_group():
    # The company list holds one row per code, so a code present under ANY
    # exporter is present — merge_by_code would fold them into one row.
    existing = [{"rex": "ITREX1", "general_code": "02013000",
                 "full_code": "0201300090", "taric_code": "90",
                 "description": "TARTARE", "lines": 4}]
    additions = [{"rex": "", "general_code": "02013000",
                  "full_code": "0201300090", "taric_code": "90",
                  "description": "BEEF", "lines": 0}]
    merged, stats = cl.add_entries(existing, additions)
    assert stats["added"] == 0 and merged == existing


def test_added_entries_survive_the_derived_round_trip(tmp_path):
    existing, _ = cl.parse_export(EXPORT_CSV.encode("cp1252"))
    additions, _ = cl.read_code_list(write_list(tmp_path))
    merged, _ = cl.add_entries(existing, additions)
    path = tmp_path / "commodity_codes.csv"
    cl.write_derived(merged, path)
    assert cl.read_derived(path) == merged
