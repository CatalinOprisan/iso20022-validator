"""The error list: every error is a readable block (full message, no truncation) and, where an editor
shows the text, its line number is a button that jumps the editor to that line.

AppTest cannot run the page's JavaScript, so the jump is checked here at the Python boundary (the right
line is handed to `editor.scroll_to_line`, once) and in a real browser in test_e2e_navigation.py.
"""

from pathlib import Path

import pytest
import streamlit as st
from helpers import current_tab, errors_shown, goto_buttons, select_tab
from streamlit.testing.v1 import AppTest

from iso20022_validator import validate_bytes, validate_file

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
V09 = SAMPLES / "pain.001.001.09"
INVALID = sorted((p for p in SAMPLES.rglob("invalid_*.xml")), key=str)


@pytest.fixture
def fake_editor(monkeypatch):
    """A text box for the Ace editor, and a recorder for scroll_to_line calls."""

    def editor(value, key, annotations=None):
        return st.text_area("XML editor", value=value, key=key, height=300)

    def scroll(line):
        st.session_state["scrolled_to"] = [*st.session_state.get("scrolled_to", []), line]

    monkeypatch.setattr("iso20022_validator.editor.xml_editor", editor)
    monkeypatch.setattr("iso20022_validator.editor.scroll_to_line", scroll)


def in_test_mode(text: str) -> AppTest:
    at = AppTest.from_file(APP).run()
    select_tab(at, "Test")
    at.text_area(key="test_editor").input(text).run()
    at.button(key="validate_btn").click().run()
    assert not at.exception
    return at


def upload(path: Path) -> AppTest:
    at = AppTest.from_file(APP).run()
    at.file_uploader[0].upload(path.name, path.read_bytes(), "text/xml").run()
    assert not at.exception
    return at


# A message with three business errors (3 different lines), to click through
THREE = (
    (V09 / "valid_single_payment.xml")
    .read_text(encoding="utf-8")
    .replace("DE89370400440532013000", "DE89370400440532013001")
    .replace("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>4</NbOfTxs>", 1)
    .replace("2099-10-05", "2020-01-01")
)


# ---- the whole message is shown ------------------------------------------------------------------


@pytest.mark.parametrize("path", INVALID, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_error_is_shown_in_full_in_upload_mode(path):
    expected = validate_file(path).errors
    rows = errors_shown(upload(path))
    assert [r["Error"] for r in rows] == [e.message for e in expected]  # not cut, not ellipsised
    assert [r["XML path"] for r in rows] == [e.path for e in expected]
    assert [(r["Type"], r["Rule"]) for r in rows] == [(e.kind, e.rule or "") for e in expected]


def test_blocks_wrap_long_text_instead_of_clipping_it():
    at = upload(V09 / "invalid_iban_checksum.xml")  # message and path are both long
    (el,) = [e for e in at.get("html") if "iso-err" in e.proto.body]
    body = el.proto.body
    assert "overflow-wrap:anywhere" in body and "white-space:pre-wrap" in body
    assert "nowrap" not in body and "text-overflow" not in body and "ellipsis" not in body
    assert "overflow:hidden" not in body.replace(" ", "")


def test_markup_in_a_message_is_shown_as_text_not_interpreted():
    xml = (V09 / "valid_single_payment.xml").read_text(encoding="utf-8").replace("Ccy=\"EUR\">1250.50<", "Ccy=\"EUR\">&lt;b&gt;&amp;co<")
    (row,) = errors_shown(upload_text(xml))
    assert "<b>&co" in row["Error"]  # shown literally ...
    at = upload_text(xml)
    (el,) = [e for e in at.get("html") if "iso-err" in e.proto.body]
    assert "<b>" not in el.proto.body  # ... because it was escaped in the HTML


def upload_text(xml: str) -> AppTest:
    at = AppTest.from_file(APP).run()
    at.file_uploader[0].upload("t.xml", xml.encode("utf-8"), "text/xml").run()
    assert not at.exception
    return at


# ---- the same list in every place that shows errors ------------------------------------------------


@pytest.mark.parametrize("path", INVALID, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_test_mode_and_upload_mode_show_the_same_errors(path, fake_editor):
    assert errors_shown(in_test_mode(path.read_text(encoding="utf-8"))) == errors_shown(upload(path))


def test_generate_and_batch_failures_use_the_same_blocks():
    at = AppTest.from_file(APP).run()
    select_tab(at, "Generate")
    template = (V09 / "valid_single_payment.xml").read_bytes()
    at.file_uploader[0].upload("t.xml", template, "text/xml").run()
    at.text_input(key="gen_amount").set_value("abc")
    at.button[0].click().run()
    rows = errors_shown(at)
    assert rows and "decimal" in rows[0]["Error"] and rows[0]["Type"] == "schema"
    assert not goto_buttons(at)  # no editor there, so no jump buttons

    at = AppTest.from_file(APP).run()
    select_tab(at, "Batch Generate")
    at.file_uploader[0].upload("t.xml", template, "text/xml").run()
    at.text_input(key="bat_amount").set_value("abc")
    at.button[0].click().run()
    rows = errors_shown(at)
    assert rows and "decimal" in rows[0]["Error"]
    assert not goto_buttons(at)


# ---- click a line number -> the editor jumps there ---------------------------------------------------


def test_each_error_has_a_button_with_its_line_number(fake_editor):
    at = in_test_mode(THREE)
    rows = errors_shown(at)
    assert len(rows) == 3
    assert [b.label for b in goto_buttons(at)] == [f"Line {r['Line']}" for r in rows]


def test_clicking_an_error_sends_exactly_its_line_to_the_editor(fake_editor):
    at = in_test_mode(THREE)
    lines = [r["Line"] for r in errors_shown(at)]
    assert len(set(lines)) == 3 and "scrolled_to" not in at.session_state  # nothing moves by itself

    for i, line in enumerate(lines):
        goto_buttons(at)[i].click().run()
        assert not at.exception
        assert at.session_state["scrolled_to"][-1] == line  # not line-1, not line+1
        assert len(at.session_state["scrolled_to"]) == i + 1  # one jump per click


def test_the_jump_happens_once_and_does_not_repeat_on_later_reruns(fake_editor):
    at = in_test_mode(THREE)
    goto_buttons(at)[1].click().run()
    assert len(at.session_state["scrolled_to"]) == 1
    at.text_area(key="test_editor").input(THREE + "\n").run()  # the user keeps typing
    at.run()
    assert len(at.session_state["scrolled_to"]) == 1  # the editor is not pulled back to the error line
    assert "goto_line" not in at.session_state


def test_a_valid_message_has_no_line_buttons(fake_editor):
    at = in_test_mode((V09 / "valid_single_payment.xml").read_text(encoding="utf-8"))
    assert errors_shown(at) == [] and goto_buttons(at) == []


@pytest.mark.parametrize("version", ["pain.001.001.03", "pain.001.001.09"])
def test_upload_mode_line_button_opens_the_file_in_the_editor_at_that_line(version, fake_editor):
    path = SAMPLES / version / "invalid_iban_checksum.xml"
    at = upload(path)
    expected = validate_file(path).errors[0]
    (button,) = goto_buttons(at)
    assert button.label == f"Line {expected.line}"

    button.click().run()
    assert not at.exception
    assert current_tab(at) == "Test"  # switched to the editor
    assert at.text_area(key="test_editor").value == path.read_text(encoding="utf-8")  # holding the file
    assert at.session_state["scrolled_to"] == [expected.line]  # at that line
    assert errors_shown(at)[0]["Line"] == expected.line  # already validated, markers and list in place
    assert errors_shown(at) == errors_shown(upload(path))


def test_opening_from_upload_gives_the_same_result_as_validating_the_text(fake_editor):
    path = V09 / "invalid_stray_text.xml"
    at = upload(path)
    goto_buttons(at)[0].click().run()
    assert errors_shown(at) == [
        {"Type": e.kind, "Rule": e.rule or "", "Line": e.line, "XML path": e.path, "Error": e.message}
        for e in validate_bytes(path.read_bytes()).errors
    ]
