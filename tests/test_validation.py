from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iso20022_validator import validate_bytes, validate_file
from iso20022_validator.cli import main

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "pain.001" / "pain.001.001.09"
APP = Path(__file__).resolve().parent.parent / "src" / "iso20022_validator" / "app.py"


def sample(name: str) -> Path:
    return SAMPLES / name


def test_valid_sample():
    result = validate_file(sample("valid_single_payment.xml"))
    assert result.valid
    assert result.format().splitlines() == ["Schema: pain.001.001.09", "VALID"]


def test_missing_mandatory_element_has_line_and_path():
    (err,) = validate_file(sample("invalid_missing_msgid.xml")).errors
    assert err.line == 4
    assert err.path == "/Document/CstmrCdtTrfInitn/GrpHdr"
    assert "MsgId" in err.message


def test_wrong_data_type():
    (err,) = validate_file(sample("invalid_wrong_amount_type.xml")).errors
    assert err.path.endswith("/Amt/InstdAmt")
    assert err.line == 39
    assert "decimal" in err.message


def test_unknown_element():
    (err,) = validate_file(sample("invalid_unknown_element.xml")).errors
    assert "Foo" in err.message
    assert err.line == 13


def test_not_wellformed_is_clear_error_not_crash():
    result = validate_file(sample("invalid_not_wellformed.xml"))
    (err,) = result.errors
    assert err.kind == "xml"
    assert err.message.startswith("Not well-formed XML")


def test_missing_file_is_reported(tmp_path):
    result = validate_file(tmp_path / "nope.xml")
    assert not result.valid
    assert "Cannot read file" in result.errors[0].message


@pytest.mark.parametrize(
    "name,code",
    [
        ("valid_single_payment.xml", 0),
        ("invalid_missing_msgid.xml", 1),
        ("invalid_wrong_amount_type.xml", 1),
        ("invalid_unknown_element.xml", 1),
        ("invalid_not_wellformed.xml", 1),
    ],
)
def test_cli_exit_code_and_output(name, code, capsys):
    assert main([str(sample(name))]) == code
    out = capsys.readouterr().out
    lines = out.splitlines()
    if name != "invalid_not_wellformed.xml":  # no schema is selected for non-XML input
        assert lines.pop(0) == "Schema: pain.001.001.09"
    assert lines[0].startswith("VALID" if code == 0 else "INVALID")


def test_web_ui_loads_without_error():
    at = AppTest.from_file(str(APP)).run()
    assert not at.exception


@pytest.mark.parametrize("path", sorted(SAMPLES.glob("*.xml")), ids=lambda p: p.name)
def test_web_ui_entry_point_matches_file_validation(path):
    """The UI calls validate_bytes on the upload; it must equal validate_file."""
    assert validate_bytes(path.read_bytes()) == validate_file(path)
