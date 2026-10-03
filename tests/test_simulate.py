"""Simulate (v0.5): a valid pain.001 in, the matching pain.002 status report out."""

import hashlib
import re
from datetime import datetime
from pathlib import Path

import pytest
from helpers import errors_shown, select_tab, tab_labels
from lxml import etree
from streamlit.testing.v1 import AppTest

from iso20022_validator import validate_bytes
from iso20022_validator.core import (
    PAIRING,
    REASON_CODES,
    Decision,
    SimulationError,
    TemplateError,
    read_original,
    read_template,
    simulate,
)
from iso20022_validator.core.engine import supported_namespaces
from iso20022_validator.core.simulate import build_pain002

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
SAMPLES = ROOT / "samples" / "pain.001"
VERSIONS = list(PAIRING)  # pain.001.001.03, pain.001.001.09
NOW = datetime(2026, 10, 3, 17, 30, 45)

ACCEPTED, REJECTED, PENDING = "Accepted (ACSC)", "Rejected (RJCT)", "Pending (PDNG)"


def sample(version: str, name: str = "valid_three_payments.xml") -> bytes:
    return (SAMPLES / version / name).read_bytes()


class Reply:
    """A pain.002 read back as the things a bank's customer would look at."""

    def __init__(self, xml: bytes):
        self.root = etree.fromstring(xml)
        self.ns = {"p": etree.QName(self.root).namespace}

    def text(self, path: str) -> str | None:
        found = self.root.xpath(path, namespaces=self.ns)
        return found[0].text if found else None

    def all(self, path: str) -> list[str]:
        return [e.text for e in self.root.xpath(path, namespaces=self.ns)]

    @property
    def transactions(self) -> list[dict]:
        rows = []
        for pmt in self.root.xpath("//p:OrgnlPmtInfAndSts", namespaces=self.ns):
            for tx in pmt.xpath("p:TxInfAndSts", namespaces=self.ns):
                one = lambda path: next(iter(tx.xpath(path, namespaces=self.ns)), None)  # noqa: E731
                rows.append(
                    {
                        "pmt_inf": pmt.xpath("string(p:OrgnlPmtInfId)", namespaces=self.ns),
                        "instr": getattr(one("p:OrgnlInstrId"), "text", None),
                        "e2e": getattr(one("p:OrgnlEndToEndId"), "text", None),
                        "uetr": getattr(one("p:OrgnlUETR"), "text", None),
                        "status": getattr(one("p:TxSts"), "text", None),
                        "reason": getattr(one("p:StsRsnInf/p:Rsn/p:Cd"), "text", None),
                    }
                )
        return rows


MIX = [Decision(), Decision("RJCT", "AM04"), Decision("PDNG")]


# ---- the pairing -------------------------------------------------------------------------------


def test_pairing_is_03_with_03_and_09_with_10():
    assert PAIRING == {"pain.001.001.03": "pain.002.001.03", "pain.001.001.09": "pain.002.001.10"}


def test_both_pain002_schemas_are_installed_as_plugins():
    versions = {ns.rsplit(":", 1)[-1] for ns in supported_namespaces()}
    assert {"pain.001.001.03", "pain.001.001.09", "pain.002.001.03", "pain.002.001.10"} <= versions


@pytest.mark.parametrize("version", VERSIONS)
def test_the_reply_uses_the_paired_schema_and_names_the_original_message_type(version):
    result = simulate(sample(version), MIX, now=NOW)
    assert result.ok and result.errors == []
    assert result.schema_version == PAIRING[version]
    check = validate_bytes(result.xml)  # its own XSD, from the namespace
    assert check.valid and check.schema_version == PAIRING[version]
    reply = Reply(result.xml)
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlMsgNmId") == version


# ---- what the reply says -----------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_mixed_statuses_reference_the_original_ids_and_carry_the_right_codes(version):
    reply = Reply(simulate(sample(version), MIX, now=NOW).xml)

    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlMsgId") == "MSG-MULTI-0001"  # the original MsgId
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlCreDtTm") == "2026-10-03T09:30:00"  # and its CreDtTm
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlNbOfTxs") == "3"
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlCtrlSum") == "1425.75"

    uetr = "9d7b2f6e-4c1a-4e3f-8a5d-2b7c9e1f0a34" if version == "pain.001.001.09" else None
    assert reply.transactions == [
        {"pmt_inf": "PMT-0001", "instr": "INSTR-0001", "e2e": "E2E-0001", "uetr": uetr, "status": "ACSC", "reason": None},
        {"pmt_inf": "PMT-0001", "instr": None, "e2e": "E2E-0002", "uetr": None, "status": "RJCT", "reason": "AM04"},
        {"pmt_inf": "PMT-0002", "instr": "INSTR-0003", "e2e": "E2E-0003", "uetr": None, "status": "PDNG", "reason": None},
    ]
    assert reply.text("//p:OrgnlGrpInfAndSts/p:GrpSts") == "PART"
    assert reply.all("//p:OrgnlGrpInfAndSts/p:NbOfTxsPerSts/p:DtldSts") == ["ACSC", "RJCT", "PDNG"]
    assert reply.all("//p:OrgnlGrpInfAndSts/p:NbOfTxsPerSts/p:DtldNbOfTxs") == ["1", "1", "1"]
    assert reply.all("//p:OrgnlPmtInfAndSts/p:PmtInfSts") == ["PART", "PDNG"]


@pytest.mark.parametrize("version", VERSIONS)
def test_everything_is_accepted_by_default(version):
    result = simulate(sample(version), now=NOW)  # no decisions at all: zero clicks
    reply = Reply(result.xml)
    assert [t["status"] for t in reply.transactions] == ["ACSC"] * 3
    assert reply.text("//p:OrgnlGrpInfAndSts/p:GrpSts") == "ACSC"
    assert reply.all("//p:StsRsnInf") == []  # no reason for an acceptance


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("decision,group", [(Decision("RJCT", "AC01"), "RJCT"), (Decision("PDNG"), "PDNG"), (Decision("ACSC"), "ACSC")])
def test_group_status_follows_the_transactions(version, decision, group):
    reply = Reply(simulate(sample(version), [decision] * 3, now=NOW).xml)
    assert reply.text("//p:OrgnlGrpInfAndSts/p:GrpSts") == group
    assert reply.all("//p:OrgnlPmtInfAndSts/p:PmtInfSts") == [group, group]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("code", list(REASON_CODES))
def test_every_offered_reason_code_gives_a_valid_reply(version, code):
    reply = Reply(simulate(sample(version, "valid_two_payments.xml"), [Decision("RJCT", code), Decision()], now=NOW).xml)
    assert [t["reason"] for t in reply.transactions] == [code, None]


def test_the_requested_reason_codes_are_offered():
    assert {"AC01", "AM04", "RC01", "MS03"} <= set(REASON_CODES)
    assert REASON_CODES["AC01"] == "Incorrect account number" and REASON_CODES["MS03"] == "Reason not specified"


@pytest.mark.parametrize("version", VERSIONS)
def test_instr_id_and_uetr_are_cited_only_when_the_original_has_them(version):
    reply = Reply(simulate(sample(version), now=NOW).xml)
    assert reply.all("//p:TxInfAndSts/p:OrgnlInstrId") == ["INSTR-0001", "INSTR-0003"]  # E2E-0002 has none
    single = Reply(simulate(sample(version, "valid_single_payment.xml"), now=NOW).xml)
    assert single.all("//p:TxInfAndSts/p:OrgnlInstrId") == []
    # OrgnlUETR is a 2019-version element: cited for .09 -> .10, not invented for .03
    assert len(reply.all("//p:TxInfAndSts/p:OrgnlUETR")) == (1 if version == "pain.001.001.09" else 0)


@pytest.mark.parametrize("version", VERSIONS)
def test_ids_are_copied_from_the_message_not_from_a_fixed_list(version):
    xml = sample(version).decode("utf-8")
    for old, new in [("MSG-MULTI-0001", "MSG-A&amp;B-77"), ("E2E-0002", "E2E-ÄÖ-<2>".replace("<", "&lt;").replace(">", "&gt;")), ("PMT-0002", "PMT-XYZ")]:
        xml = xml.replace(old, new)
    result = simulate(xml.encode("utf-8"), now=NOW)
    reply = Reply(result.xml)
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlMsgId") == "MSG-A&B-77"
    assert [t["e2e"] for t in reply.transactions] == ["E2E-0001", "E2E-ÄÖ-<2>", "E2E-0003"]
    assert [t["pmt_inf"] for t in reply.transactions] == ["PMT-0001", "PMT-0001", "PMT-XYZ"]
    assert validate_bytes(result.xml).valid


@pytest.mark.parametrize("version", VERSIONS)
def test_the_reply_has_its_own_new_header(version):
    a, b = simulate(sample(version), now=NOW), simulate(sample(version), now=NOW)
    reply = Reply(a.xml)
    assert reply.text("//p:GrpHdr/p:MsgId") == a.msg_id != "MSG-MULTI-0001"
    assert re.fullmatch(r"STS-20261003-173045-[A-Z0-9]{4}", a.msg_id) and len(a.msg_id) <= 35
    assert reply.text("//p:GrpHdr/p:CreDtTm") == "2026-10-03T17:30:45"
    assert a.msg_id != b.msg_id  # two replies never share a MsgId
    assert a.xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')


# ---- the safety net and the refusals -------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_an_invalid_reply_is_never_returned(version):
    result = simulate(sample(version), [Decision("RJCT", "TOOLONG"), Decision(), Decision()], now=NOW)
    assert not result.ok and result.xml is None  # no file
    assert result.errors and all(e.kind == "schema" for e in result.errors)
    assert result.errors[0].path.endswith("/Rsn/Cd") and result.errors[0].line
    assert result.schema_version == PAIRING[version]


def test_decisions_are_checked():
    with pytest.raises(ValueError, match="needs a reason"):
        Decision("RJCT")
    with pytest.raises(ValueError, match="only a rejected"):
        Decision("ACSC", "AC01")
    with pytest.raises(ValueError, match="status must be"):
        Decision("MAYBE")
    with pytest.raises(ValueError, match="3 transactions but 1 decisions"):
        simulate(sample("pain.001.001.09"), [Decision()])


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize(
    "name,fragment",
    [
        ("invalid_missing_msgid.xml", "MsgId"),  # schema error
        ("invalid_ctrlsum_mismatch.xml", "CtrlSum"),  # business-rule error
        ("invalid_past_date.xml", "past"),
        ("invalid_not_wellformed.xml", "well-formed"),
    ],
)
def test_an_invalid_pain001_is_refused_with_its_errors(version, name, fragment):
    with pytest.raises(SimulationError) as exc:
        simulate(sample(version, name))
    assert exc.value.errors and fragment in exc.value.errors[0].message


def test_only_a_pain001_can_be_answered():
    reply = simulate(sample("pain.001.001.09")).xml
    with pytest.raises(SimulationError, match="needs a pain.001"):
        simulate(reply)
    with pytest.raises(TemplateError, match="must be a pain.001"):  # same for the generators
        read_template(reply)


def test_read_original_lists_every_transaction_in_order():
    original = read_original(sample("pain.001.001.09"))
    assert (original.msg_id, original.created, original.response_version) == ("MSG-MULTI-0001", "2026-10-03T09:30:00", "pain.002.001.10")
    assert [(t.pmt_inf_id, t.end_to_end_id, t.instr_id, t.amount, t.currency) for t in original.transactions] == [
        ("PMT-0001", "E2E-0001", "INSTR-0001", "1250.50", "EUR"),
        ("PMT-0001", "E2E-0002", None, "100.00", "EUR"),
        ("PMT-0002", "E2E-0003", "INSTR-0003", "75.25", "EUR"),
    ]
    assert build_pain002(original, [Decision()] * 3, NOW)[0]


# ---- pain.002 as a file to validate (the engine knows the new schemas) ---------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_a_generated_reply_validates_as_a_file_without_business_rules(version):
    xml = simulate(sample(version), MIX, now=NOW).xml
    result = validate_bytes(xml)
    assert result.valid and result.schema_version == PAIRING[version]
    assert result.business_rules_apply is False and result.business_checked is False
    assert result.format().splitlines() == [f"Schema: {PAIRING[version]}", "VALID"]


@pytest.mark.parametrize("version", VERSIONS)
def test_a_broken_reply_is_a_schema_error_and_does_not_talk_about_business_rules(version):
    xml = simulate(sample(version), now=NOW).xml.decode().replace("<OrgnlMsgId>MSG-MULTI-0001</OrgnlMsgId>", "")
    result = validate_bytes(xml.encode())
    assert not result.valid and {e.kind for e in result.errors} == {"schema"}
    assert "OrgnlMsgId" in result.errors[0].message
    assert "Business rules" not in result.format()


# ---- the Simulate mode of the app ------------------------------------------------------------------------


def open_simulate(version: str, name: str = "valid_three_payments.xml") -> tuple[AppTest, str]:
    at = AppTest.from_file(APP).run()
    select_tab(at, "Simulate")
    data = sample(version, name)
    at.file_uploader[0].upload(name, data, "text/xml").run()
    assert not at.exception
    return at, hashlib.sha256(data).hexdigest()[:12]


def sim_boxes(at):
    """The status/reason dropdowns of the Simulate form (the sidebar has selectboxes of its own)."""
    return [s for s in at.selectbox if s.key.startswith("sim_")]


def status_box(at, digest, i):
    return at.selectbox(key=f"sim_{digest}_status_{i}")


def reason_box(at, digest, i):
    return at.selectbox(key=f"sim_{digest}_reason_{i}")


def generated(at: AppTest) -> Reply:
    return Reply(at.session_state["sim_result"][1].xml)


def test_simulate_is_a_mode_next_to_the_others():
    at = AppTest.from_file(APP).run()
    assert tab_labels(at) == ["Upload file", "Test", "Generate", "Batch Generate", "Simulate"]


@pytest.mark.parametrize("version", VERSIONS)
def test_the_transactions_are_listed_with_everything_accepted(version):
    at, digest = open_simulate(version)
    assert any(f"MSG-MULTI-0001 ({version}" in i.value and PAIRING[version] in i.value for i in at.info)
    assert len(sim_boxes(at)) == 6  # a status and a reason for each of the 3 transactions
    html_blocks = " ".join(e.proto.body for e in at.get("html"))
    for e2e in ("E2E-0001", "E2E-0002", "E2E-0003"):
        assert e2e in html_blocks
    assert "INSTR-0001" in html_blocks and "PMT-0002" in html_blocks and "Second Supplier BV" in html_blocks
    for i in range(3):
        assert status_box(at, digest, i).value == ACCEPTED  # the default
        assert reason_box(at, digest, i).disabled  # a reason only makes sense for a rejection


@pytest.mark.parametrize("version", VERSIONS)
def test_zero_clicks_gives_an_accepted_reply(version):
    at, _ = open_simulate(version)
    at.button(key="sim_generate").click().run()
    assert not at.exception
    assert any(f"Generated a valid {PAIRING[version]} reply" in s.value for s in at.success)
    assert at.get("download_button")
    assert [t["status"] for t in generated(at).transactions] == ["ACSC"] * 3


@pytest.mark.parametrize("version", VERSIONS)
def test_a_mix_chosen_in_the_form_ends_up_in_the_reply(version):
    at, digest = open_simulate(version)
    status_box(at, digest, 1).select(REJECTED).run()
    assert not reason_box(at, digest, 1).disabled  # enabled once rejected
    assert reason_box(at, digest, 0).disabled
    reason_box(at, digest, 1).select("RC01 · Bank identifier incorrect").run()
    status_box(at, digest, 2).select(PENDING).run()
    at.button(key="sim_generate").click().run()

    assert not at.exception
    reply = generated(at)
    assert [(t["e2e"], t["status"], t["reason"]) for t in reply.transactions] == [
        ("E2E-0001", "ACSC", None), ("E2E-0002", "RJCT", "RC01"), ("E2E-0003", "PDNG", None),
    ]
    assert reply.text("//p:OrgnlGrpInfAndSts/p:OrgnlMsgId") == "MSG-MULTI-0001"
    assert validate_bytes(at.session_state["sim_result"][1].xml).valid
    assert at.get("download_button")


def test_a_reason_that_is_chosen_but_not_rejected_is_ignored():
    at, digest = open_simulate("pain.001.001.09")
    status_box(at, digest, 0).select(REJECTED).run()
    reason_box(at, digest, 0).select("AM04 · Insufficient funds").run()
    status_box(at, digest, 0).select(ACCEPTED).run()  # changed our mind
    at.button(key="sim_generate").click().run()
    assert [t["reason"] for t in generated(at).transactions] == [None, None, None]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("name", ["invalid_missing_msgid.xml", "invalid_ctrlsum_mismatch.xml", "invalid_past_date.xml"])
def test_an_invalid_pain001_shows_its_errors_and_stops(version, name):
    at, _ = open_simulate(version, name)
    assert any("not valid" in e.value for e in at.error)
    rows = errors_shown(at)
    assert rows and rows[0]["Type"] in {"schema", "business"}
    assert len(sim_boxes(at)) == 0 and not at.get("download_button")  # nothing to simulate
    assert not [b for b in at.button if b.key == "sim_generate"]


def test_uploading_another_message_drops_the_old_result():
    at, _ = open_simulate("pain.001.001.09")
    at.button(key="sim_generate").click().run()
    assert at.get("download_button")
    other = sample("pain.001.001.03", "valid_two_payments.xml")
    at.file_uploader[0].upload("other.xml", other, "text/xml").run()
    assert not at.get("download_button") and len(sim_boxes(at)) == 4  # 2 transactions, nothing generated yet


def test_a_reply_uploaded_in_upload_mode_is_valid_and_says_there_are_no_business_rules():
    xml = simulate(sample("pain.001.001.09"), MIX, now=NOW).xml
    at = AppTest.from_file(APP).run()
    at.file_uploader[0].upload("reply.xml", xml, "text/xml").run()
    assert not at.exception
    assert [s.value for s in at.success] == ["VALID"]
    assert "Schema used: pain.002.001.10" in [i.value for i in at.info]
    assert any("no business rules" in c.value.lower() for c in at.caption)


def test_a_broken_reply_in_upload_mode_does_not_claim_business_rules_were_skipped():
    xml = simulate(sample("pain.001.001.03"), now=NOW).xml.replace(b"<TxSts>ACSC</TxSts>", b"<TxSts>ACSC</TxSts><Foo/>", 1)
    at = AppTest.from_file(APP).run()
    at.file_uploader[0].upload("reply.xml", xml, "text/xml").run()
    assert at.error and errors_shown(at)
    assert not any("Business rules" in i.value for i in at.info)
