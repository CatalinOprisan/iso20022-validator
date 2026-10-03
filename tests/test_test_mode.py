"""The "Test" mode: an edit-and-revalidate workspace (schema + business rules) around the XML editor.

Streamlit's AppTest cannot drive a custom component, so these tests swap the Ace editor for a plain
text box that records the markers it was given. The real editor was checked separately in a browser.
"""

import re
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from helpers import errors_shown, goto_buttons
from iso20022_validator import validate_file
from iso20022_validator.editor import annotations_for

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
CASES = sorted(SAMPLES.rglob("*.xml"), key=str)
CASE_IDS = [f"{p.parent.name}/{p.name}" for p in CASES]
MESSAGE = "Type or paste an XML message in the editor, then click Validate."
GREEN, RED = "#1a7f37", "#cf222e"


@pytest.fixture
def plain_editor(monkeypatch):
    def fake(value, key, annotations=None):
        st.session_state["markers_given_to_editor"] = annotations or []
        return st.text_area("XML editor", value=value, key=key, height=300)

    monkeypatch.setattr("iso20022_validator.editor.xml_editor", fake)


def open_test_mode() -> AppTest:
    at = AppTest.from_file(APP).run()
    at.radio[0].set_value("Test").run()
    assert not at.exception
    return at


def validate(at: AppTest, text: str) -> AppTest:
    at.text_area(key="test_editor").input(text).run()
    at.button(key="validate_btn").click().run()
    assert not at.exception
    return at


def table(at: AppTest) -> list[dict]:
    return errors_shown(at)


def expected_rows(path: Path) -> list[dict]:
    return [
        {"Type": e.kind, "Rule": e.rule or "", "Line": e.line, "XML path": e.path, "Error": e.message}
        for e in validate_file(path).errors
    ]


def button_color(at: AppTest) -> str | None:
    styles = [m.value for m in at.markdown if ".st-key-validate_btn" in m.value]
    return next((c for c in (GREEN, RED) if styles and c in styles[0]), None)


# ---- naming ---------------------------------------------------------------------------------


def test_the_mode_is_called_test_not_paste():
    at = AppTest.from_file(APP).run()
    assert at.radio[0].options == ["Upload file", "Test", "Generate", "Batch Generate"]
    at.radio[0].set_value("Test").run()
    shown = " ".join(w.label for w in at.text_area) + " " + " ".join(b.label for b in at.button)
    assert "paste" not in shown.lower()


def test_real_ace_editor_renders_without_error():
    at = AppTest.from_file(APP).run()
    at.radio[0].set_value("Test").run()  # real st_ace component, not the stand-in
    assert not at.exception
    assert at.button(key="validate_btn").label == "Validate"


# ---- same results as the file path ----------------------------------------------------------


@pytest.mark.parametrize("path", CASES, ids=CASE_IDS)
def test_test_mode_gives_the_same_result_as_validating_the_file(path, plain_editor):
    expected = validate_file(path)
    at = validate(open_test_mode(), path.read_text(encoding="utf-8"))

    infos = [i.value for i in at.info]
    if expected.schema_version:
        assert f"Schema used: {expected.schema_version}" in infos
    else:
        assert not any(v.startswith("Schema used") for v in infos)

    if expected.valid:
        assert [s.value for s in at.success] == ["VALID"]
        assert errors_shown(at) == []
    else:
        assert at.error[0].value == f"INVALID — {len(expected.errors)} error(s)"
        assert table(at) == expected_rows(path)
        skipped = "Business rules were not checked: fix the schema errors first."
        assert (skipped in infos) == (bool(expected.schema_version) and not expected.business_checked)


# ---- validation is on demand ----------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\t  "])
def test_empty_editor_gives_a_neutral_prompt(text, plain_editor):
    at = open_test_mode()
    assert [i.value for i in at.info] == [MESSAGE]  # before any click
    validate(at, text)
    assert [i.value for i in at.info] == [MESSAGE]
    assert not at.success and not at.error and errors_shown(at) == []


def test_nothing_is_validated_until_validate_is_clicked(plain_editor):
    at = open_test_mode()
    at.text_area(key="test_editor").input((SAMPLES / "pain.001.001.09" / "valid_single_payment.xml").read_text(encoding="utf-8")).run()
    assert not at.success and not at.error and errors_shown(at) == []
    at.button(key="validate_btn").click().run()
    assert [s.value for s in at.success] == ["VALID"]


def test_edit_and_revalidate_workflow(plain_editor):
    at = open_test_mode()
    broken = (SAMPLES / "pain.001.001.09" / "invalid_ctrlsum_mismatch.xml").read_text(encoding="utf-8")
    validate(at, broken)
    assert at.error and table(at)[0]["Rule"] == "ctrl_sum"
    assert button_color(at) == RED

    fixed = broken.replace("<CtrlSum>1200.75</CtrlSum>", "<CtrlSum>1250.50</CtrlSum>")
    at.text_area(key="test_editor").input(fixed).run()  # editing alone does not re-validate ...
    assert not at.error and not at.success and errors_shown(at) == []  # ... and the stale result is hidden
    assert any("changed since it was last validated" in c.value for c in at.caption)
    assert button_color(at) is None

    at.button(key="validate_btn").click().run()  # clicking checks the new text
    assert [s.value for s in at.success] == ["VALID"]
    assert button_color(at) == GREEN


def test_validating_twice_in_a_row_is_stable(plain_editor):
    at = open_test_mode()
    validate(at, (SAMPLES / "pain.001.001.03" / "invalid_past_date.xml").read_text(encoding="utf-8"))
    first = table(at)
    at.button(key="validate_btn").click().run()
    assert table(at) == first and len(at.error) == 1


# ---- schema and business rules both apply -----------------------------------------------------


def test_business_errors_are_tagged_as_business_with_their_rule(plain_editor):
    at = validate(open_test_mode(), (SAMPLES / "pain.001.001.03" / "invalid_iban_checksum.xml").read_text(encoding="utf-8"))
    (row,) = table(at)
    assert (row["Type"], row["Rule"]) == ("business", "iban_checksum")
    assert "mod-97" in row["Error"]


def test_schema_errors_are_tagged_as_schema_and_block_business_rules(plain_editor):
    at = validate(open_test_mode(), (SAMPLES / "pain.001.001.09" / "invalid_wrong_amount_type.xml").read_text(encoding="utf-8"))
    (row,) = table(at)
    assert (row["Type"], row["Rule"]) == ("schema", "")
    assert "Business rules were not checked: fix the schema errors first." in [i.value for i in at.info]


# ---- line numbers match the editor -------------------------------------------------------------


def last_segment(path: str) -> str:
    return re.sub(r"\[\d+\]$", "", path.rsplit("/", 1)[-1])


@pytest.mark.parametrize("path", [p for p in CASES if p.name.startswith("invalid_") and "not_wellformed" not in p.name and "unsupported" not in p.name], ids=lambda p: f"{p.parent.name}/{p.name}")
def test_error_lines_point_at_the_offending_element_in_the_editor_text(path, plain_editor):
    text = path.read_text(encoding="utf-8")
    at = validate(open_test_mode(), text)
    lines = text.splitlines()
    for row in table(at):
        if "character data" in row["Error"]:  # reported on the line of the stray text itself (see test_line_numbers)
            assert "stray text" in lines[row["Line"] - 1], row
        else:
            assert f"<{last_segment(row['XML path'])}" in lines[row["Line"] - 1], row


def test_line_numbers_shift_with_the_editor_content(plain_editor):
    text = (SAMPLES / "pain.001.001.09" / "invalid_ctrlsum_mismatch.xml").read_text(encoding="utf-8")
    body = text.split("?>", 1)[1].lstrip()  # no declaration, so leading blank lines are legal
    shifted = "\n\n\n" + body
    at = validate(open_test_mode(), shifted)
    (row,) = table(at)
    assert shifted.splitlines()[row["Line"] - 1].strip() == "<CtrlSum>1200.75</CtrlSum>"
    assert row["Line"] == body.splitlines().index(next(l for l in body.splitlines() if "1200.75" in l)) + 1 + 3


@pytest.mark.parametrize("path", [p for p in CASES if p.name.startswith("invalid_")], ids=lambda p: f"{p.parent.name}/{p.name}")
def test_the_editor_gets_a_marker_on_every_error_line(path, plain_editor):
    at = validate(open_test_mode(), path.read_text(encoding="utf-8"))
    lines = [row["Line"] for row in table(at)]
    markers = at.session_state["markers_given_to_editor"]
    assert [m["row"] + 1 for m in markers] == lines  # Ace rows are 0-based, lines 1-based
    assert all(m["type"] == "error" and m["text"] for m in markers)


def test_markers_disappear_when_the_text_is_edited(plain_editor):
    at = validate(open_test_mode(), (SAMPLES / "pain.001.001.09" / "invalid_ctrlsum_mismatch.xml").read_text(encoding="utf-8"))
    assert at.session_state["markers_given_to_editor"]
    at.text_area(key="test_editor").input("<Document/>").run()
    assert at.session_state["markers_given_to_editor"] == []


def test_annotations_for_skips_errors_without_a_line():
    class E:
        def __init__(self, line, tag="schema", message="m"):
            self.line, self.tag, self.message = line, tag, message

    assert annotations_for([E(5, "business/ctrl_sum", "bad"), E(None)]) == [
        {"row": 4, "column": 0, "type": "error", "text": "[business/ctrl_sum] bad"}
    ]


# ---- the editor keeps its content ---------------------------------------------------------------


def test_editor_content_is_kept_between_validations_and_mode_switches(plain_editor):
    at = open_test_mode()
    text = (SAMPLES / "pain.001.001.09" / "invalid_missing_msgid.xml").read_text(encoding="utf-8")
    validate(at, text)
    assert at.text_area(key="test_editor").value == text
    at.button(key="validate_btn").click().run()
    assert at.text_area(key="test_editor").value == text  # still there after a second validation

    at.radio[0].set_value("Upload file").run()
    assert not at.exception
    at.radio[0].set_value("Test").run()
    assert at.text_area(key="test_editor").value == text  # and after leaving and coming back
    assert at.error and table(at)  # the result for that exact text is shown again


# ---- other modes show the same two-layer result ---------------------------------------------------


def test_upload_mode_reports_business_errors_too():
    at = AppTest.from_file(APP).run()
    path = SAMPLES / "pain.001.001.09" / "invalid_nboftxs_mismatch.xml"
    at.file_uploader[0].upload(path.name, path.read_bytes(), "text/xml").run()
    assert not at.exception
    assert table(at) == expected_rows(path)
    assert {r["Type"] for r in table(at)} == {"business"}
