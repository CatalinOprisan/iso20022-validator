"""Business rules (v0.4): CtrlSum, NbOfTxs, IBAN checksum, execution date; both pain.001 versions."""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from iso20022_validator import validate_bytes, validate_file
from iso20022_validator.cli import main
from iso20022_validator.core.engine import validate_schema
from iso20022_validator.core.rules import iban_check_digits, iban_mod97_ok

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "pain.001"
VERSIONS = ["pain.001.001.03", "pain.001.001.09"]
TODAY = date(2026, 10, 3)  # fixed, so no test depends on the real date


def sample(version: str, name: str = "valid_single_payment.xml") -> Path:
    return SAMPLES / version / name


def text(version: str, name: str = "valid_single_payment.xml") -> str:
    return sample(version, name).read_text(encoding="utf-8")


def check(xml: str, today: date = TODAY):
    return validate_bytes(xml.encode("utf-8"), today=today)


def only_error(result):
    assert len(result.errors) == 1, [str(e) for e in result.errors]
    return result.errors[0]


# ---- every rule: one sample that violates it, one that satisfies it ---------------------------

VIOLATIONS = [
    # sample file, rule, element the error points at, fragment of the message
    ("invalid_ctrlsum_mismatch.xml", "ctrl_sum", "CtrlSum", "CtrlSum is 1200.75 but transactions sum to 1250.50"),
    ("invalid_nboftxs_mismatch.xml", "nb_of_txs", "NbOfTxs", "NbOfTxs is 2 but the message contains 1 transaction"),
    ("invalid_iban_checksum.xml", "iban_checksum", "IBAN", "DE89370400440532013001 fails the mod-97 checksum"),
    ("invalid_past_date.xml", "execution_date_past", None, "ReqdExctnDt 2020-01-01 is in the past"),
]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("name,rule,element,fragment", VIOLATIONS)
def test_violating_sample_is_reported_by_exactly_its_rule(version, name, rule, element, fragment):
    result = validate_file(sample(version, name))
    err = only_error(result)
    assert result.schema_version == version and result.business_checked
    assert not result.valid
    assert (err.kind, err.rule, err.tag) == ("business", rule, f"business/{rule}")
    assert fragment in err.message
    assert err.line and err.path.startswith("/Document/CstmrCdtTrfInitn/")
    if element:
        assert err.path.endswith("/" + element)
    # the reported line really is the offending element
    assert f"<{err.path.split('/')[-1]}" in text(version, name).splitlines()[err.line - 1]


@pytest.mark.parametrize("version", VERSIONS)
def test_satisfying_samples_are_valid_in_every_layer(version):
    for name in ("valid_single_payment.xml", "valid_two_payments.xml"):
        result = validate_file(sample(version, name))
        assert result.valid and result.business_checked and result.errors == []
        assert result.format().splitlines() == [f"Schema: {version}", "VALID"]


# ---- CtrlSum --------------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_ctrl_sum_message_names_both_numbers(version):
    xml = text(version).replace("1250.50", "1200.75").replace(
        "<CtrlSum>1200.75</CtrlSum>", "<CtrlSum>1250.75</CtrlSum>"
    )
    errors = check(xml).errors  # GrpHdr and PmtInf both wrong
    assert [e.rule for e in errors] == ["ctrl_sum", "ctrl_sum"]
    assert errors[0].message == "CtrlSum is 1250.75 but transactions sum to 1200.75"


@pytest.mark.parametrize("version", VERSIONS)
def test_ctrl_sum_in_pmtinf_is_checked_separately(version):
    xml = text(version)
    first, second = [m.start() for m in re.finditer("<CtrlSum>", xml)]
    xml = xml[:second] + xml[second:].replace("<CtrlSum>1250.50", "<CtrlSum>9.99", 1)
    err = only_error(check(xml))
    assert err.rule == "ctrl_sum" and err.path.endswith("/PmtInf/CtrlSum")


@pytest.mark.parametrize("version", VERSIONS)
def test_ctrl_sum_sums_all_transactions(version):
    ok = text(version, "valid_two_payments.xml")  # 1250.50 + 100.00 = 1350.50
    assert check(ok).valid
    err = only_error(check(ok.replace("<CtrlSum>1350.50</CtrlSum>", "<CtrlSum>1250.50</CtrlSum>", 1)))
    assert err.message == "CtrlSum is 1250.50 but transactions sum to 1350.50"


@pytest.mark.parametrize("declared,ok", [("1250.50", True), ("1250.5", True), ("1250.504", True), ("1250.51", False), ("1250.00", False)])
@pytest.mark.parametrize("version", VERSIONS)
def test_ctrl_sum_allows_decimal_rounding_only(version, declared, ok):
    xml = text(version).replace("<CtrlSum>1250.50</CtrlSum>", f"<CtrlSum>{declared}</CtrlSum>")
    assert check(xml).valid is ok


# ---- NbOfTxs --------------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_nb_of_txs_counts_transactions(version):
    two = text(version, "valid_two_payments.xml")
    assert check(two).valid  # 2 declared, 2 present
    err = only_error(check(two.replace("<NbOfTxs>2</NbOfTxs>", "<NbOfTxs>3</NbOfTxs>", 1)))
    assert err.message == "NbOfTxs is 3 but the message contains 2 transactions"
    assert err.path.endswith("/GrpHdr/NbOfTxs")


@pytest.mark.parametrize("version", VERSIONS)
def test_nb_of_txs_in_pmtinf_is_checked_separately(version):
    xml = text(version)
    head, tail = xml.split("<PmtInf>")
    err = only_error(check(head + "<PmtInf>" + tail.replace("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>5</NbOfTxs>", 1)))
    assert err.path.endswith("/PmtInf/NbOfTxs")
    assert err.message == "NbOfTxs is 5 but this PmtInf contains 1 transaction"


# ---- IBAN checksum --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "iban,ok",
    [
        ("DE89370400440532013000", True),
        ("GB82WEST12345698765432", True),
        ("FR1420041010050500013M02606", True),  # letters in the account number
        ("NL91ABNA0417164300", True),
        ("RO49AAAA1B31007593840000", True),
        ("DE89370400440532013001", False),
        ("GB82WEST12345698765433", False),
        ("DE00370400440532013000", False),
        ("NL91ABNA0417164301", False),
    ],
)
def test_mod97(iban, ok):
    assert iban_mod97_ok(iban) is ok
    if ok:
        assert iban_check_digits(iban) == iban[2:4]


@pytest.mark.parametrize("version", VERSIONS)
def test_every_iban_in_the_message_is_checked(version):
    xml = text(version)
    both = xml.replace("DE89370400440532013000", "DE89370400440532013001").replace(
        "FR1420041010050500013M02606", "FR1420041010050500013M02607"
    )
    errors = check(both).errors
    assert [e.rule for e in errors] == ["iban_checksum", "iban_checksum"]
    assert errors[0].path.endswith("/DbtrAcct/Id/IBAN") and errors[1].path.endswith("/CdtrAcct/Id/IBAN")
    only_creditor = only_error(check(xml.replace("FR1420041010050500013M02606", "FR1420041010050500013M02607")))
    assert only_creditor.path.endswith("/CdtrAcct/Id/IBAN")


@pytest.mark.parametrize("version", VERSIONS)
def test_iban_with_bad_format_is_a_schema_error_not_a_checksum_error(version):
    result = check(text(version).replace("DE89370400440532013000", "XX"))
    err = only_error(result)
    assert err.kind == "schema" and err.rule is None  # format is the XSD's job
    assert not result.business_checked


# ---- execution date -------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_execution_date_must_not_be_in_the_past(version):
    xml = text(version).replace("2099-10-05", "2026-10-05")
    assert check(xml, today=date(2026, 10, 5)).valid  # today is fine
    assert check(xml, today=date(2026, 10, 4)).valid  # future is fine
    err = only_error(check(xml, today=date(2026, 10, 6)))  # yesterday is not
    assert err.rule == "execution_date_past"
    assert err.message == "ReqdExctnDt 2026-10-05 is in the past (today is 2026-10-06)"


def test_execution_date_uses_the_real_date_by_default():
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    xml = text("pain.001.001.09")
    assert validate_bytes(xml.replace("2099-10-05", tomorrow).encode()).valid
    assert not validate_bytes(xml.replace("2099-10-05", yesterday).encode()).valid


def test_execution_date_with_time_and_timezone_in_009():
    xml = text("pain.001.001.09")
    as_datetime = xml.replace("<Dt>2099-10-05</Dt>", "<DtTm>2020-01-01T10:00:00</DtTm>")
    err = only_error(check(as_datetime))
    assert err.rule == "execution_date_past" and err.path.endswith("/ReqdExctnDt/DtTm")
    assert check(xml.replace("2099-10-05", "2099-10-05+01:00")).valid


@pytest.mark.parametrize("version", VERSIONS)
def test_every_pmtinf_date_is_checked(version):
    two_blocks = text(version, "valid_single_payment.xml")
    pmt = re.search(r"    <PmtInf>.*</PmtInf>\n", two_blocks, re.S).group(0)
    second = pmt.replace("2099-10-05", "2020-01-01")
    xml = two_blocks.replace(pmt, pmt + second).replace("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>2</NbOfTxs>", 1)
    xml = xml.replace("<CtrlSum>1250.50</CtrlSum>", "<CtrlSum>2501.00</CtrlSum>", 1)
    result = check(xml)
    err = only_error(result)
    assert err.rule == "execution_date_past" and "/PmtInf[2]/" in err.path  # indexed when there are several


# ---- layering and reporting -----------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_business_rules_run_only_after_the_schema_passes(version):
    broken = text(version).replace("<CtrlSum>1250.50</CtrlSum>", "<CtrlSum>1.00</CtrlSum>").replace("EUR", "euro")
    result = check(broken)
    assert not result.valid and not result.business_checked
    assert {e.kind for e in result.errors} == {"schema"}  # the CtrlSum problem is not reported yet
    assert "Business rules: not checked" in result.format()


@pytest.mark.parametrize("version", VERSIONS)
def test_schema_layer_alone_does_not_see_business_errors_but_validate_does(version):
    data = sample(version, "invalid_ctrlsum_mismatch.xml").read_bytes()
    assert validate_schema(data).valid  # XSD passes ...
    assert not validate_bytes(data).valid  # ... but the message is NOT valid


@pytest.mark.parametrize("version", VERSIONS)
def test_a_message_is_never_called_valid_if_a_business_rule_fails(version):
    for name, *_ in VIOLATIONS:
        result = validate_file(sample(version, name))
        assert not result.valid
        assert result.format().splitlines()[1].startswith("INVALID")


def test_all_business_errors_in_one_message_are_reported_together():
    xml = (
        text("pain.001.001.09")
        .replace("DE89370400440532013000", "DE89370400440532013001")
        .replace("<NbOfTxs>1</NbOfTxs>", "<NbOfTxs>4</NbOfTxs>", 1)
        .replace("2099-10-05", "2020-01-01")
    )
    errors = check(xml).errors
    assert {e.rule for e in errors} == {"nb_of_txs", "iban_checksum", "execution_date_past"}
    assert [e.line for e in errors] == sorted(e.line for e in errors)


@pytest.mark.parametrize("version", VERSIONS)
def test_cli_marks_business_errors_and_exits_1(version, capsys):
    assert main([str(sample(version, "invalid_ctrlsum_mismatch.xml"))]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Schema: {version}"
    assert lines[1].startswith("INVALID (1 error)")
    assert lines[2].startswith("  [business/ctrl_sum] line 8 /Document/CstmrCdtTrfInitn/GrpHdr/CtrlSum: CtrlSum is 1200.75")


def test_cli_marks_schema_errors_and_says_business_rules_were_skipped(capsys):
    assert main([str(sample("pain.001.001.09", "invalid_missing_msgid.xml"))]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[2].startswith("  [schema] line 4 /Document/CstmrCdtTrfInitn/GrpHdr:")
    assert out[-1] == "Business rules: not checked (fix the schema errors first)"
