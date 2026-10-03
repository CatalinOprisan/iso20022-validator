"""Pins that the line shown in the error table is the line of the offending text, and that the editor
marker lands on that same line (not line-1, not line+1).

Regression: stray text after </Nm> was reported on the *parent's* start tag (line 9, `<InitgPty>`)
instead of on the line holding the text (line 10), so the table and the gutter marker both pointed
one line too high. The editor mapping itself (Ace row = line - 1) was never wrong.
"""

from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from helpers import errors_shown
from iso20022_validator import validate_bytes, validate_file
from iso20022_validator.editor import annotations_for

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
VERSIONS = ["pain.001.001.03", "pain.001.001.09"]

# The pinned case: <Nm>Acme Corp</Nm>stray text   <- line 10; its parent <InitgPty> starts on line 9
STRAY_LINE = 10


def stray_sample(version: str) -> Path:
    return SAMPLES / version / "invalid_stray_text.xml"


# ---- the validator reports the line of the text itself ----------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_stray_text_is_reported_on_its_own_line_not_its_parents(version):
    path = stray_sample(version)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert "stray text" in lines[STRAY_LINE - 1] and "<InitgPty>" in lines[STRAY_LINE - 2]  # the fixture is what we think

    (err,) = validate_file(path).errors
    assert "character data" in err.message
    assert err.path.endswith("/GrpHdr/InitgPty")  # the element that holds the text keeps being the path
    assert err.line == STRAY_LINE  # not 9


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    "old,new",
    [
        ("<Nm>Acme Corp</Nm>\n      </InitgPty>", "<Nm>Acme Corp</Nm>stray text\n      </InitgPty>"),  # after a child
        ("<InitgPty>\n", "<InitgPty>stray text\n"),  # right after the start tag: the parent's own line
        ("<Nm>Acme Corp</Nm>\n      </InitgPty>", "<Nm>Acme Corp</Nm>\n\n\n   stray text\n      </InitgPty>"),  # lines later
        ("<Nm>Acme Corp</Nm>\n      </InitgPty>", "<Nm>Acme Corp</Nm>\n      </InitgPty>stray text"),  # after the parent closes
    ],
    ids=["after-child", "after-start-tag", "lines-later", "after-parent"],
)
def test_the_reported_line_always_holds_the_stray_text(version, old, new):
    xml = (SAMPLES / version / "valid_single_payment.xml").read_text(encoding="utf-8")
    assert old in xml
    broken = xml.replace(old, new, 1)
    result = validate_bytes(broken.encode("utf-8"))
    assert result.errors
    err = result.errors[0]
    assert "character data" in err.message
    assert "stray text" in broken.splitlines()[err.line - 1]


def test_other_errors_keep_their_lines():
    """The fix only touches character-data errors: the pinned lines of the other kinds are unchanged."""
    pinned = [
        ("pain.001.001.09", "invalid_missing_msgid.xml", 4),
        ("pain.001.001.09", "invalid_unknown_element.xml", 13),
        ("pain.001.001.09", "invalid_wrong_amount_type.xml", 39),
        ("pain.001.001.03", "invalid_wrong_amount_type.xml", 37),
    ]
    for version, name, line in pinned:
        assert validate_file(SAMPLES / version / name).errors[0].line == line, (version, name)


# ---- the editor marker is on the same line as the table row --------------------------------------------


def test_annotation_row_is_the_table_line_minus_one_for_the_ace_api():
    class E:
        tag, message = "schema", "stray"

        def __init__(self, line):
            self.line = line

    (marker,) = annotations_for([E(STRAY_LINE)])
    assert marker["row"] == 9  # Ace counts rows from 0 ...
    assert marker["row"] + 1 == STRAY_LINE  # ... and shows them as line numbers from 1


@pytest.fixture
def plain_editor(monkeypatch):
    def fake(value, key, annotations=None):
        st.session_state["markers_given_to_editor"] = annotations or []
        return st.text_area("XML editor", value=value, key=key, height=300)

    monkeypatch.setattr("iso20022_validator.editor.xml_editor", fake)


@pytest.mark.parametrize("version", VERSIONS)
def test_marker_lands_on_the_line_shown_in_the_table(version, plain_editor):
    text = stray_sample(version).read_text(encoding="utf-8")
    at = AppTest.from_file(APP).run()
    at.radio[0].set_value("Test").run()
    at.text_area(key="test_editor").input(text).run()
    at.button(key="validate_btn").click().run()
    assert not at.exception

    (row,) = errors_shown(at)
    (marker,) = at.session_state["markers_given_to_editor"]
    lines = text.splitlines()

    assert row["Line"] == STRAY_LINE
    assert marker["row"] + 1 == row["Line"]  # same line as the table, exactly
    assert "stray text" in lines[marker["row"]]  # and that line is the one with the text ...
    assert "stray text" not in lines[marker["row"] - 1]  # ... not the line above
    assert "stray text" not in lines[marker["row"] + 1]  # ... nor the line below
