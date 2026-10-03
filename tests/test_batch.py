"""Batch test-suite generation: library (core.batch) and the Streamlit "Batch Generate" mode."""

import csv
import io
import re
import zipfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree
from streamlit.testing.v1 import AppTest

from helpers import select_tab
from iso20022_validator import validate_bytes
from iso20022_validator.core import (
    ERROR_TYPES,
    MAX_FILES,
    BatchError,
    ErrorType,
    TemplateError,
    generate_batch,
    read_template,
)
from iso20022_validator.core.batch import invalid_count

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
VERSIONS = ["pain.001.001.03", "pain.001.001.09"]


def template_bytes(version: str, name: str = "valid_single_payment.xml") -> bytes:
    return (SAMPLES / version / name).read_bytes()


def open_zip(result) -> tuple[dict[str, bytes], list[dict[str, str]]]:
    """(files by name, manifest rows) read back from the zip bytes only."""
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes)) as zf:
        files = {n: zf.read(n) for n in zf.namelist() if n != "manifest.csv"}
        rows = list(csv.DictReader(io.StringIO(zf.read("manifest.csv").decode("utf-8"))))
    return files, rows


def ids_of(xml: bytes) -> tuple[str | None, str | None]:
    root = etree.fromstring(xml)
    ns = {"p": etree.QName(root).namespace}
    msg = root.xpath("//p:GrpHdr/p:MsgId/text()", namespaces=ns)
    e2e = root.xpath("//p:CdtTrfTxInf[1]/p:PmtId/p:EndToEndId/text()", namespaces=ns)
    return (msg[0] if msg else None, e2e[0] if e2e else None)


# ---- counts, ratio -------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("count", [1, 2, 10, 37, 100])
def test_n_files_in_zip(version, count):
    result = generate_batch(template_bytes(version), count=count, seed=count)
    files, rows = open_zip(result)
    assert len(files) == count == len(result.files)
    assert len(rows) == count


@pytest.mark.parametrize(
    "count,valid_percent,expected_invalid",
    [(10, 80, 2), (10, 100, 0), (10, 0, 10), (100, 80, 20), (7, 80, 1), (5, 50, 3), (1, 80, 0), (1, 20, 1), (33, 90, 3)],
)
def test_valid_invalid_ratio_is_respected(count, valid_percent, expected_invalid):
    result = generate_batch(template_bytes("pain.001.001.09"), count=count, valid_percent=valid_percent, seed=7)
    invalid = sum(1 for f in result.files if not f.valid)
    assert invalid == expected_invalid == invalid_count(count, valid_percent)
    assert abs(invalid - count * (100 - valid_percent) / 100) <= 0.5  # "within rounding"
    _, rows = open_zip(result)
    assert sum(r["status"] == "invalid" for r in rows) == expected_invalid


def test_default_ratio_is_80_20():
    result = generate_batch(template_bytes("pain.001.001.03"), count=50)
    assert sum(not f.valid for f in result.files) == 10


@pytest.mark.parametrize("count", [0, -1, MAX_FILES + 1])
def test_file_count_is_bounded(count):
    with pytest.raises(ValueError):
        generate_batch(template_bytes("pain.001.001.09"), count=count)


@pytest.mark.parametrize("percent", [-1, 100.5])
def test_valid_percent_is_bounded(percent):
    with pytest.raises(ValueError):
        generate_batch(template_bytes("pain.001.001.09"), count=5, valid_percent=percent)


# ---- every file is what the manifest says ---------------------------------------------------

ERROR_PATH_END = {
    "invalid_iban": ("IBAN",),
    "invalid_amount": ("InstdAmt",),
    "malformed_date": ("ReqdExctnDt", "Dt"),
}


@pytest.mark.parametrize("version", VERSIONS)
def test_valid_files_pass_and_invalid_files_fail_the_validator(version):
    result = generate_batch(template_bytes(version), count=60, valid_percent=50, seed=3)
    files, rows = open_zip(result)
    assert {r["filename"] for r in rows} == set(files)
    for row in rows:
        check = validate_bytes(files[row["filename"]])
        assert check.schema_version == version
        if row["status"] == "valid":
            assert check.valid, (row["filename"], [str(e) for e in check.errors])
        else:
            assert not check.valid, row["filename"]
            # exactly the one injected error, and it is the kind of error the manifest claims
            assert len(check.errors) == 1, (row["filename"], [str(e) for e in check.errors])
            err = check.errors[0]
            assert err.kind == "schema"
            kind = row["error_type"]
            if kind == "missing_field":
                assert "MsgId" in err.message or "EndToEndId" in err.message
            else:
                assert err.path.split("/")[-1] in ERROR_PATH_END[kind], (kind, err.path)


def test_manifest_describes_every_file():
    result = generate_batch(template_bytes("pain.001.001.09"), count=40, valid_percent=50, seed=11)
    files, rows = open_zip(result)
    known = {e.name for e in ERROR_TYPES}
    assert list(rows[0]) == ["filename", "status", "error_type", "detail", "schema_version"]
    assert [r["filename"] for r in rows] == [f.filename for f in result.files]  # one row per file, in order
    assert len(rows) == len(files) == 40
    for row, f in zip(rows, result.files):
        assert row["status"] in {"valid", "invalid"}
        assert (row["status"] == "valid") == f.valid
        assert row["schema_version"] == "pain.001.001.09"
        if row["status"] == "valid":
            assert row["error_type"] == "" and row["detail"] == ""
            assert row["filename"].endswith("_valid.xml")
        else:
            assert row["error_type"] in known and row["detail"]
            assert row["filename"].endswith(f"_{row['error_type']}.xml")
    assert result.manifest == rows


def test_error_types_are_spread_over_the_invalid_files():
    result = generate_batch(template_bytes("pain.001.001.09"), count=40, valid_percent=0, seed=5)
    used = {f.error_type for f in result.files}
    assert used == {e.name for e in ERROR_TYPES}
    assert {e.name for e in ERROR_TYPES} == {"invalid_iban", "invalid_amount", "missing_field", "malformed_date"}


# ---- valid files are realistic and distinct --------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_ids_are_unique_and_amounts_vary(version):
    result = generate_batch(template_bytes(version), count=100, valid_percent=100, seed=1)
    msg_ids, e2e_ids, amounts = [], [], set()
    for f in result.files:
        msg, e2e = ids_of(f.xml)
        msg_ids.append(msg)
        e2e_ids.append(e2e)
        amount = read_template(f.xml).fields.amount
        assert re.fullmatch(r"\d+\.\d{2}", amount) and Decimal(amount) > 0
        amounts.add(amount)
    assert len(set(msg_ids)) == len(set(e2e_ids)) == 100
    assert not set(msg_ids) & set(e2e_ids)
    assert not {"MSG-2026-0001", "MSG-2026-0003"} & set(msg_ids)  # not the template's own
    assert len(amounts) > 50


def test_ids_are_unique_across_a_batch_with_invalid_files_too():
    result = generate_batch(template_bytes("pain.001.001.09"), count=100, valid_percent=20, seed=2)
    msgs = [m for m, _ in (ids_of(f.xml) for f in result.files) if m]
    assert len(msgs) == len(set(msgs))


def test_control_sum_follows_the_amount_for_single_payment_templates():
    for f in generate_batch(template_bytes("pain.001.001.09"), count=10, valid_percent=100, seed=4).files:
        amount = read_template(f.xml).fields.amount
        assert re.findall(r"<CtrlSum>(.*?)</CtrlSum>", f.xml.decode()) == [amount, amount]


def test_control_sum_includes_the_untouched_second_payment():
    for f in generate_batch(template_bytes("pain.001.001.03", "valid_two_payments.xml"), count=5, valid_percent=100, seed=4).files:
        total = str(Decimal(read_template(f.xml).fields.amount) + Decimal("100.00"))
        assert re.findall(r"<CtrlSum>(.*?)</CtrlSum>", f.xml.decode()) == [total, total]


def test_form_values_are_used_in_every_file():
    base = replace(
        read_template(template_bytes("pain.001.001.09")).fields,
        debtor_name="Batch Debtor", creditor_iban="GB29NWBK60161331926819", currency="USD", execution_date="2099-02-01",
    )
    result = generate_batch(template_bytes("pain.001.001.09"), base, count=10, valid_percent=100, seed=9)
    for f in result.files:
        fields = read_template(f.xml).fields
        assert (fields.debtor_name, fields.creditor_iban, fields.currency, fields.execution_date) == (
            "Batch Debtor", "GB29NWBK60161331926819", "USD", "2099-02-01",
        )


def test_amount_varies_around_the_base_amount():
    base = replace(read_template(template_bytes("pain.001.001.09")).fields, amount="1000.00")
    result = generate_batch(template_bytes("pain.001.001.09"), base, count=100, valid_percent=100, seed=6)
    amounts = [Decimal(read_template(f.xml).fields.amount) for f in result.files]
    assert all(Decimal("100.00") <= a <= Decimal("2000.00") for a in amounts)


def test_same_seed_gives_the_same_plan():
    a = generate_batch(template_bytes("pain.001.001.09"), count=30, seed=42)
    b = generate_batch(template_bytes("pain.001.001.09"), count=30, seed=42)
    plan = lambda r: [(f.filename, f.detail, read_template(f.xml).fields.amount if f.valid else None) for f in r.files]
    assert plan(a) == plan(b)
    c = generate_batch(template_bytes("pain.001.001.09"), count=30, seed=43)
    assert plan(a) != plan(c)


# ---- failures and extensibility --------------------------------------------------------------


def test_base_values_that_make_an_invalid_message_produce_no_zip():
    base = replace(read_template(template_bytes("pain.001.001.09")).fields, amount="abc")
    with pytest.raises(BatchError) as exc:
        generate_batch(template_bytes("pain.001.001.09"), base, count=5)
    assert exc.value.errors and "decimal" in exc.value.errors[0].message


def test_invalid_template_is_refused():
    with pytest.raises(TemplateError):
        generate_batch((SAMPLES / "pain.001.001.09" / "invalid_missing_msgid.xml").read_bytes(), count=5)


def test_a_new_error_type_can_be_added_by_registering_it():
    def too_long_msg_id(root, els, rng):
        els["msg_id"].text = "X" * 36  # Max35Text
        return "MsgId longer than 35 characters"

    custom = ErrorType("long_msg_id", "MsgId over 35 chars", too_long_msg_id)
    result = generate_batch(template_bytes("pain.001.001.09"), count=6, valid_percent=0, seed=1, error_types=[custom])
    files, rows = open_zip(result)
    assert {r["error_type"] for r in rows} == {"long_msg_id"}
    assert all(not validate_bytes(x).valid for x in files.values())


def test_an_error_type_that_does_not_break_the_message_is_never_shipped():
    noop = ErrorType("noop", "does nothing", lambda root, els, rng: "nothing")
    with pytest.raises(BatchError):
        generate_batch(template_bytes("pain.001.001.09"), count=3, valid_percent=0, error_types=[noop])


# ---- Streamlit "Batch Generate" mode ---------------------------------------------------------


def open_batch(version="pain.001.001.09", name="valid_single_payment.xml") -> AppTest:
    at = AppTest.from_file(APP).run()
    select_tab(at, "Batch Generate")
    at.file_uploader[0].upload(name, template_bytes(version, name), "text/xml").run()
    assert not at.exception
    return at


def test_ui_batch_form_has_fields_and_controls_with_defaults():
    at = open_batch()
    assert at.text_input(key="bat_debtor_name").value == "Acme Corp"
    assert at.text_input(key="bat_amount").value == "1250.50"
    assert at.number_input(key="bat_count").value == 10
    assert at.slider(key="bat_valid_percent").value == 80
    assert not any(t.key in ("bat_msg_id", "bat_end_to_end_id") for t in at.text_input)  # IDs are per file
    assert "Template schema: pain.001.001.09" in [i.value for i in at.info]


@pytest.mark.parametrize("version", VERSIONS)
def test_ui_generates_a_zip_with_manifest(version):
    at = open_batch(version)
    at.number_input(key="bat_count").set_value(20)
    at.slider(key="bat_valid_percent").set_value(75)
    at.button[0].click().run()
    assert not at.exception
    result = at.session_state["bat_result"]
    files, rows = open_zip(result)
    assert len(files) == len(rows) == 20
    assert sum(r["status"] == "invalid" for r in rows) == 5
    assert at.get("download_button"), "a zip must be offered"
    assert any("20" in s.value and "15 valid, 5 invalid" in s.value for s in at.success)
    shown = at.dataframe[0].value.to_dict("records")
    assert [r["filename"] for r in shown] == [r["filename"] for r in rows]


def test_ui_uses_the_edited_form_values():
    at = open_batch()
    at.text_input(key="bat_debtor_name").set_value("Typed Debtor")
    at.text_input(key="bat_currency").set_value("CHF")
    at.number_input(key="bat_count").set_value(3)
    at.slider(key="bat_valid_percent").set_value(100)
    at.button[0].click().run()
    for f in at.session_state["bat_result"].files:
        fields = read_template(f.xml).fields
        assert (fields.debtor_name, fields.currency) == ("Typed Debtor", "CHF")


def test_ui_bad_base_value_shows_errors_and_no_zip():
    at = open_batch()
    at.text_input(key="bat_amount").set_value("abc")
    at.button[0].click().run()
    assert not at.exception
    assert any("No zip was produced" in e.value for e in at.error)
    assert not at.get("download_button")


def test_ui_invalid_template_is_rejected():
    at = AppTest.from_file(APP).run()
    select_tab(at, "Batch Generate")
    bad = (SAMPLES / "pain.001.001.09" / "invalid_missing_msgid.xml").read_bytes()
    at.file_uploader[0].upload("bad.xml", bad, "text/xml").run()
    assert not at.exception
    assert any("not a valid pain.001 message" in e.value for e in at.error)
    assert len(at.text_input) == 0


def test_single_generate_mode_still_works_next_to_batch():
    at = AppTest.from_file(APP).run()
    select_tab(at, "Generate")
    at.file_uploader[0].upload("t.xml", template_bytes("pain.001.001.09"), "text/xml").run()
    at.button[0].click().run()
    assert not at.exception
    assert any("Generated a valid" in s.value for s in at.success)
