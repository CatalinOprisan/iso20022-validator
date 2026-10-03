"""Message generation: library (core.generator) and the Streamlit "Generate" mode."""

import re
from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree
from streamlit.testing.v1 import AppTest

from helpers import errors_shown
from iso20022_validator import validate_bytes
from iso20022_validator.core import (
    TemplateError,
    generate,
    new_end_to_end_id,
    new_msg_id,
    read_template,
)

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
VERSIONS = ["pain.001.001.03", "pain.001.001.09"]

ID_PATTERN = r"{prefix}-\d{{8}}-\d{{6}}-[A-Z0-9]{{4}}"


def template_bytes(version: str, name: str = "valid_single_payment.xml") -> bytes:
    return (SAMPLES / version / name).read_bytes()


EDITS = dict(
    msg_id="MSG-EDITED-1",
    debtor_name="New Debtor GmbH",
    debtor_iban="NL91ABNA0417164300",
    creditor_name="New Creditor & Sons <Ltd>",  # characters that need XML escaping
    creditor_iban="GB29NWBK60161331926819",
    amount="99.99",
    currency="USD",
    execution_date="2099-01-15",
    end_to_end_id="E2E-EDITED-1",
)


# ---- library ------------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_template_fields_are_read(version):
    tpl = read_template(template_bytes(version))
    assert tpl.schema_version == version
    assert tpl.transaction_count == 1
    f = tpl.fields
    assert (f.debtor_name, f.creditor_name) == ("Acme Corp", "Supplier SARL")
    assert (f.amount, f.currency, f.execution_date) == ("1250.50", "EUR", "2099-10-05")
    assert f.debtor_iban == "DE89370400440532013000"


@pytest.mark.parametrize("version", VERSIONS)
def test_editing_fields_produces_valid_output(version):
    data = template_bytes(version)
    tpl = read_template(data)
    result = generate(data, replace(tpl.fields, **EDITS))
    assert result.ok and result.errors == []
    assert result.schema_version == version  # version of the template is kept
    check = validate_bytes(result.xml)
    assert check.valid and check.schema_version == version
    # and the new values are really in there
    assert read_template(result.xml).fields == replace(tpl.fields, **EDITS)


@pytest.mark.parametrize("version", VERSIONS)
def test_unedited_generation_reproduces_template_byte_for_byte(version):
    data = template_bytes(version)
    assert generate(data, read_template(data).fields).xml == data


@pytest.mark.parametrize("version", VERSIONS)
def test_only_the_edited_fields_differ_from_the_template(version):
    data = template_bytes(version)
    result = generate(data, replace(read_template(data).fields, **EDITS))

    def xpath(el):
        names = [etree.QName(a).localname for a in reversed(list(el.iterancestors()))]
        return "/".join(names + [etree.QName(el).localname])

    def flat(xml):
        return [
            (xpath(t), t.attrib.get("Ccy"), (t.text or "").strip(), (t.tail or "").strip())
            for t in etree.fromstring(xml).iter()
        ]

    before, after = flat(data), flat(result.xml)
    assert [p for p, *_ in before] == [p for p, *_ in after]  # identical structure
    changed = {b[0].split("/")[-1] for b, a in zip(before, after) if b != a}
    date_tag = "ReqdExctnDt" if version.endswith("03") else "Dt"
    # CtrlSum is the one extra change: it follows the edited amount so the message stays consistent
    assert changed == {"MsgId", "Nm", "IBAN", "InstdAmt", date_tag, "EndToEndId", "CtrlSum"}
    assert re.findall(r"<CtrlSum>(.*?)</CtrlSum>", result.xml.decode()) == ["99.99", "99.99"]
    # everything outside those elements (e.g. CreDtTm, PmtMtd, BICs, RmtInf) is untouched
    for tag in ("CreDtTm", "PmtMtd", "NbOfTxs", "BIC", "BICFI", "Ustrd", "PmtInfId"):
        pattern = rf"<{tag}>.*?</{tag}>"
        assert re.findall(pattern, data.decode()) == re.findall(pattern, result.xml.decode())
    assert result.xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')


@pytest.mark.parametrize(
    "field,value,fragment",
    [
        ("amount", "twelve", "decimal"),
        ("amount", "", "decimal"),
        ("creditor_iban", "NOT-AN-IBAN", "pattern"),
        ("debtor_iban", "12345", "pattern"),
        ("currency", "euro", "pattern"),
        ("execution_date", "05/10/2026", "date"),
        ("msg_id", "", ""),
    ],
)
@pytest.mark.parametrize("version", VERSIONS)
def test_invalid_value_is_caught_and_no_xml_is_returned(version, field, value, fragment):
    data = template_bytes(version)
    result = generate(data, replace(read_template(data).fields, **{field: value}))
    assert not result.ok
    assert result.xml is None
    assert result.errors and fragment in result.errors[0].message
    assert result.errors[0].line  # reported like any other validation error


def test_invalid_template_is_refused_with_its_errors():
    bad = (SAMPLES / "pain.001.001.09" / "invalid_missing_msgid.xml").read_bytes()
    with pytest.raises(TemplateError) as exc:
        read_template(bad)
    assert exc.value.errors and "MsgId" in exc.value.errors[0].message


def test_unsupported_namespace_template_is_refused():
    bad = (SAMPLES / "pain.001.001.03" / "invalid_unsupported_namespace.xml").read_bytes()
    with pytest.raises(TemplateError) as exc:
        read_template(bad)
    assert "Unsupported namespace" in exc.value.errors[0].message


@pytest.mark.parametrize("version", VERSIONS)
def test_multi_transaction_template_edits_only_the_first(version):
    data = template_bytes(version, "valid_two_payments.xml")
    tpl = read_template(data)
    assert tpl.transaction_count == 2
    result = generate(data, replace(tpl.fields, end_to_end_id="E2E-FIRST", amount="5.00"))
    assert result.ok
    text = result.xml.decode()
    assert "E2E-FIRST" in text and "E2E-0002" in text  # second transaction untouched
    assert ">100.00<" in text


def test_generated_ids_follow_the_pattern_and_differ():
    msg, e2e = new_msg_id(), new_end_to_end_id()
    assert re.fullmatch(ID_PATTERN.format(prefix="MSG"), msg)
    assert re.fullmatch(ID_PATTERN.format(prefix="E2E"), e2e)
    assert msg != e2e and len(msg) <= 35 and len(e2e) <= 35


# ---- Streamlit "Generate" mode -------------------------------------------------------------


def open_generate(version: str, name: str = "valid_single_payment.xml") -> AppTest:
    at = AppTest.from_file(APP).run()
    at.radio[0].set_value("Generate").run()
    at.file_uploader[0].upload(name, template_bytes(version, name), "text/xml").run()
    assert not at.exception
    return at


def submit(at: AppTest) -> AppTest:
    """Click Generate. Form widgets commit on submit, so edits are set without a run() before this."""
    return at.button[0].click().run()


@pytest.mark.parametrize("version", VERSIONS)
def test_ui_form_is_prefilled_from_template(version):
    at = open_generate(version)
    assert at.text_input(key="gen_debtor_name").value == "Acme Corp"
    assert at.text_input(key="gen_creditor_iban").value == "FR1420041010050500013M02606"
    assert at.text_input(key="gen_amount").value == "1250.50"
    assert at.text_input(key="gen_currency").value == "EUR"
    assert str(at.date_input(key="gen_execution_date").value) == "2099-10-05"
    assert f"Template schema: {version}" in [i.value for i in at.info]


@pytest.mark.parametrize("version", VERSIONS)
def test_ui_ids_are_generated_not_taken_from_template(version):
    at = open_generate(version)
    assert re.fullmatch(ID_PATTERN.format(prefix="MSG"), at.text_input(key="gen_msg_id").value)
    assert re.fullmatch(ID_PATTERN.format(prefix="E2E"), at.text_input(key="gen_end_to_end_id").value)
    assert at.text_input(key="gen_msg_id").value != "MSG-2026-0001"


@pytest.mark.parametrize("version", VERSIONS)
def test_ui_two_messages_from_same_template_have_different_ids(version):
    at = open_generate(version)
    submit(at)
    first = read_template(at.session_state["gen_result"][0].xml).fields
    submit(at)
    second = read_template(at.session_state["gen_result"][0].xml).fields
    assert first.msg_id != second.msg_id
    assert first.end_to_end_id != second.end_to_end_id
    assert first.msg_id != first.end_to_end_id


def test_generating_twice_from_a_fresh_session_also_differs():
    a, b = open_generate("pain.001.001.09"), open_generate("pain.001.001.09")
    assert a.text_input(key="gen_msg_id").value != b.text_input(key="gen_msg_id").value
    assert a.text_input(key="gen_end_to_end_id").value != b.text_input(key="gen_end_to_end_id").value


def test_ui_ids_stay_editable_and_are_used_as_typed():
    at = open_generate("pain.001.001.03")
    at.text_input(key="gen_msg_id").set_value("MY-OWN-ID")
    at.text_input(key="gen_end_to_end_id").set_value("MY-E2E")
    submit(at)
    fields = read_template(at.session_state["gen_result"][0].xml).fields
    assert (fields.msg_id, fields.end_to_end_id) == ("MY-OWN-ID", "MY-E2E")
    # an overridden ID is kept for the next message (user may want to reuse it)
    assert at.text_input(key="gen_msg_id").value == "MY-OWN-ID"


@pytest.mark.parametrize("version", VERSIONS)
def test_ui_generate_offers_download_of_valid_xml(version):
    at = open_generate(version)
    at.text_input(key="gen_amount").set_value("42.00")
    at.text_input(key="gen_currency").set_value("CHF")
    submit(at)
    assert not at.exception
    assert any(f"Generated a valid {version} message." in s.value for s in at.success)
    assert at.get("download_button"), "valid result must offer a download"
    xml = at.session_state["gen_result"][0].xml
    assert validate_bytes(xml).valid
    assert 'Ccy="CHF">42.00<' in xml.decode()


@pytest.mark.parametrize("field,value", [("gen_amount", "abc"), ("gen_creditor_iban", "BAD IBAN")])
def test_ui_invalid_value_shows_errors_and_no_download(field, value):
    at = open_generate("pain.001.001.09")
    at.text_input(key=field).set_value(value)
    submit(at)
    assert not at.exception
    assert any("not valid" in e.value for e in at.error)
    assert not at.get("download_button")
    assert errors_shown(at)  # the errors are listed


def test_ui_multi_transaction_note_is_shown():
    at = open_generate("pain.001.001.09", "valid_two_payments.xml")
    assert any("multi-transaction editing comes later" in c.value for c in at.caption)
    single = open_generate("pain.001.001.09")
    assert not any("multi-transaction" in c.value for c in single.caption)


def test_ui_invalid_template_is_rejected():
    at = AppTest.from_file(APP).run()
    at.radio[0].set_value("Generate").run()
    bad = (SAMPLES / "pain.001.001.09" / "invalid_missing_msgid.xml").read_bytes()
    at.file_uploader[0].upload("bad.xml", bad, "text/xml").run()
    assert not at.exception
    assert any("not a valid pain.001 message" in e.value for e in at.error)
    assert len(at.text_input) == 0  # no form
