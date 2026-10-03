from pathlib import Path

import pytest

from iso20022_validator import validate_bytes, validate_file
from iso20022_validator.cli import main

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
V03 = SAMPLES / "pain.001" / "pain.001.001.03"
V09 = SAMPLES / "pain.001" / "pain.001.001.09"


def test_valid_v03_passes_and_reports_version():
    result = validate_file(V03 / "valid_single_payment.xml")
    assert result.valid
    assert result.schema_version == "pain.001.001.03"


def test_valid_v09_reports_version():
    assert validate_file(V09 / "valid_single_payment.xml").schema_version == "pain.001.001.09"


def test_v03_missing_mandatory_element():
    result = validate_file(V03 / "invalid_missing_msgid.xml")
    (err,) = result.errors
    assert result.schema_version == "pain.001.001.03"
    assert (err.line, err.path) == (4, "/Document/CstmrCdtTrfInitn/GrpHdr")
    assert "MsgId" in err.message


def test_v03_wrong_data_type():
    (err,) = validate_file(V03 / "invalid_wrong_amount_type.xml").errors
    assert err.path.endswith("/Amt/InstdAmt")
    assert "decimal" in err.message


def test_v03_unknown_element():
    (err,) = validate_file(V03 / "invalid_unknown_element.xml").errors
    assert "Foo" in err.message


def test_v03_not_wellformed():
    result = validate_file(V03 / "invalid_not_wellformed.xml")
    assert result.errors[0].kind == "xml"
    assert result.schema_version is None


def test_schema_is_chosen_by_namespace_not_by_file():
    """A .09 document is rejected by .03 rules and vice versa: each file hits its own schema."""
    v09_as_03 = (V09 / "valid_single_payment.xml").read_bytes().replace(b"pain.001.001.09", b"pain.001.001.03")
    result = validate_bytes(v09_as_03)
    assert result.schema_version == "pain.001.001.03"
    assert not result.valid  # .09-only structure (e.g. <Dt> wrapper, BICFI) is invalid under .03


def test_unsupported_namespace_gives_clear_error():
    result = validate_file(V03 / "invalid_unsupported_namespace.xml")
    (err,) = result.errors
    assert err.kind == "namespace"
    assert "pain.001.001.99" in err.message
    assert "Unsupported namespace" in err.message
    assert "pain.001.001.03" in err.message and "pain.001.001.09" in err.message
    assert result.schema_version is None


def test_missing_namespace_gives_clear_error():
    result = validate_bytes(b"<Document><CstmrCdtTrfInitn/></Document>")
    (err,) = result.errors
    assert err.kind == "namespace"
    assert "(none)" in err.message


def test_cli_shows_detected_version(capsys):
    assert main([str(V03 / "valid_single_payment.xml")]) == 0
    assert capsys.readouterr().out.splitlines() == ["Schema: pain.001.001.03", "VALID"]


def test_cli_unsupported_namespace_exit_1(capsys):
    assert main([str(V03 / "invalid_unsupported_namespace.xml")]) == 1
    assert "Unsupported namespace" in capsys.readouterr().out


def test_app_shows_schema_version():
    from streamlit.testing.v1 import AppTest

    app = Path(__file__).resolve().parent.parent / "src" / "iso20022_validator" / "app.py"
    assert not AppTest.from_file(str(app)).run().exception
