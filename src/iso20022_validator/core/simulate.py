"""Simulate the bank's reply: from a valid pain.001, build the matching pain.002 (Customer Payment Status Report).

The user picks a status per transaction (accepted / rejected with a reason code / pending). The reply is
built in the pain.002 version that pairs with the pain.001 version (PAIRING) and, like generated pain.001
messages, is validated against its own XSD before it is returned: no XML comes out unless it is valid.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from lxml import etree

from .engine import NS_PREFIX, validate_bytes
from .errors import ValidationError
from .generator import generate_id

ACCEPTED, REJECTED, PENDING = "ACSC", "RJCT", "PDNG"
STATUSES = {
    ACCEPTED: "Accepted (ACSC)",
    REJECTED: "Rejected (RJCT)",
    PENDING: "Pending (PDNG)",
}

# ExternalStatusReason1Code values offered for a rejection (code -> ISO name). Any 1-4 character code is
# valid in the XSD, so `Decision` accepts others too; these are the common ones for payment initiation.
REASON_CODES = {
    "AC01": "Incorrect account number",
    "AC04": "Closed account number",
    "AC06": "Blocked account",
    "AG01": "Transaction forbidden",
    "AM04": "Insufficient funds",
    "AM05": "Duplication",
    "BE01": "Inconsistent with end customer",
    "DT01": "Invalid date",
    "MS03": "Reason not specified",
    "RC01": "Bank identifier incorrect",
}

# Which pain.002 answers which pain.001 (see the README for the sources).
PAIRING = {
    "pain.001.001.03": "pain.002.001.03",
    "pain.001.001.09": "pain.002.001.10",
}


class SimulationError(Exception):
    """The pain.001 cannot be answered (not valid, or not a supported pain.001)."""

    def __init__(self, message: str, errors: list[ValidationError] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class Decision:
    """What the 'bank' says about one transaction."""

    status: str = ACCEPTED
    reason: str | None = None  # required for a rejection, not allowed otherwise

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}, not {self.status!r}")
        if self.status == REJECTED and not self.reason:
            raise ValueError("a rejected transaction needs a reason code")
        if self.status != REJECTED and self.reason:
            raise ValueError("only a rejected transaction carries a reason code")


@dataclass(frozen=True)
class OriginalTransaction:
    pmt_inf_id: str
    end_to_end_id: str
    instr_id: str | None
    uetr: str | None
    amount: str
    currency: str
    creditor_name: str


@dataclass(frozen=True)
class OriginalMessage:
    schema_version: str  # e.g. "pain.001.001.09"
    msg_id: str
    created: str  # GrpHdr/CreDtTm as written
    nb_of_txs: str | None
    ctrl_sum: str | None
    transactions: list[OriginalTransaction]

    @property
    def response_version(self) -> str:
        return PAIRING[self.schema_version]


@dataclass(frozen=True)
class SimulationResult:
    xml: bytes | None  # None whenever the reply is not valid
    errors: list[ValidationError]
    schema_version: str  # the pain.002 version that was built
    msg_id: str  # MsgId of the reply

    @property
    def ok(self) -> bool:
        return self.xml is not None


def read_original(data: bytes, *, today=None) -> OriginalMessage:
    """Check that `data` is a valid pain.001 (schema AND business rules) and read what the reply must cite."""
    check = validate_bytes(data, today=today)
    if not check.valid:
        raise SimulationError("The pain.001 message is not valid. Fix it before simulating a reply.", check.errors)
    if check.schema_version not in PAIRING:
        raise SimulationError(f"Simulate needs a pain.001 message, not {check.schema_version}.")

    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    ns = {"p": etree.QName(root).namespace}
    text = lambda el, path: (el.findtext(path, namespaces=ns) or "").strip() or None  # noqa: E731
    header = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", ns)

    transactions = []
    for pmt in root.findall("p:CstmrCdtTrfInitn/p:PmtInf", ns):
        for tx in pmt.findall("p:CdtTrfTxInf", ns):
            amount = tx.find("p:Amt/p:InstdAmt", ns)
            if amount is None:
                amount = tx.find("p:Amt/p:EqvtAmt/p:Amt", ns)
            transactions.append(
                OriginalTransaction(
                    pmt_inf_id=text(pmt, "p:PmtInfId"),
                    end_to_end_id=text(tx, "p:PmtId/p:EndToEndId"),
                    instr_id=text(tx, "p:PmtId/p:InstrId"),
                    uetr=text(tx, "p:PmtId/p:UETR"),
                    amount=amount.text.strip(),
                    currency=amount.get("Ccy", ""),
                    creditor_name=text(tx, "p:Cdtr/p:Nm") or "",
                )
            )
    return OriginalMessage(
        schema_version=check.schema_version,
        msg_id=text(header, "p:MsgId"),
        created=text(header, "p:CreDtTm"),
        nb_of_txs=text(header, "p:NbOfTxs"),
        ctrl_sum=text(header, "p:CtrlSum"),
        transactions=transactions,
    )


def _group_status(statuses: Sequence[str]) -> str:
    """Group/payment-information status from the transaction statuses: all alike, otherwise partially accepted."""
    return statuses[0] if len(set(statuses)) == 1 else "PART"


def build_pain002(original: OriginalMessage, decisions: Sequence[Decision], now: datetime | None = None) -> tuple[bytes, str]:
    """The pain.002 XML (not validated) and its MsgId. One decision per transaction, in the original's order."""
    if len(decisions) != len(original.transactions):
        raise ValueError(f"{len(original.transactions)} transactions but {len(decisions)} decisions")
    now = (now or datetime.now()).replace(microsecond=0)
    version = original.response_version
    ns = NS_PREFIX + version

    def add(parent, tag, text=None):
        el = etree.SubElement(parent, f"{{{ns}}}{tag}")
        if text is not None:
            el.text = text
        return el

    document = etree.Element(f"{{{ns}}}Document", nsmap={None: ns})
    report = add(document, "CstmrPmtStsRpt")

    msg_id = generate_id("STS", now)
    header = add(report, "GrpHdr")
    add(header, "MsgId", msg_id)
    add(header, "CreDtTm", now.isoformat())

    statuses = [d.status for d in decisions]
    group = add(report, "OrgnlGrpInfAndSts")
    add(group, "OrgnlMsgId", original.msg_id)
    add(group, "OrgnlMsgNmId", original.schema_version)
    add(group, "OrgnlCreDtTm", original.created)
    if original.nb_of_txs:
        add(group, "OrgnlNbOfTxs", original.nb_of_txs)
    if original.ctrl_sum:
        add(group, "OrgnlCtrlSum", original.ctrl_sum)
    add(group, "GrpSts", _group_status(statuses))
    for status in STATUSES:  # a count per status that occurs
        if status in statuses:
            per_status = add(group, "NbOfTxsPerSts")
            add(per_status, "DtldNbOfTxs", str(statuses.count(status)))
            add(per_status, "DtldSts", status)

    by_payment: dict[str, list[tuple[OriginalTransaction, Decision]]] = {}
    for tx, decision in zip(original.transactions, decisions):
        by_payment.setdefault(tx.pmt_inf_id, []).append((tx, decision))
    for pmt_inf_id, items in by_payment.items():
        payment = add(report, "OrgnlPmtInfAndSts")
        add(payment, "OrgnlPmtInfId", pmt_inf_id)
        add(payment, "PmtInfSts", _group_status([d.status for _, d in items]))
        for tx, decision in items:
            info = add(payment, "TxInfAndSts")
            if tx.instr_id:
                add(info, "OrgnlInstrId", tx.instr_id)
            add(info, "OrgnlEndToEndId", tx.end_to_end_id)
            if tx.uetr and version == "pain.002.001.10":  # OrgnlUETR exists from the 2019 version on
                add(info, "OrgnlUETR", tx.uetr)
            add(info, "TxSts", decision.status)
            if decision.status == REJECTED:
                reason = add(add(info, "StsRsnInf"), "Rsn")
                add(reason, "Cd", decision.reason)

    body = etree.tostring(document, encoding="UTF-8", xml_declaration=False, pretty_print=True)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + body, msg_id


def simulate(
    data: bytes,
    decisions: Sequence[Decision] | None = None,
    *,
    now: datetime | None = None,
    today=None,
) -> SimulationResult:
    """Valid pain.001 in, matching pain.002 out (every transaction accepted unless `decisions` says otherwise).

    Raises SimulationError if the pain.001 is not valid. The reply is validated against its own XSD; if it is
    not valid (e.g. a reason code longer than 4 characters) no XML is returned, only the errors.
    """
    original = read_original(data, today=today)
    decisions = list(decisions) if decisions is not None else [Decision()] * len(original.transactions)
    xml, msg_id = build_pain002(original, decisions, now)
    check = validate_bytes(xml)
    if not check.valid:
        return SimulationResult(None, check.errors, original.response_version, msg_id)
    return SimulationResult(xml, [], original.response_version, msg_id)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def file_name(result: SimulationResult) -> str:
    return _SAFE_NAME.sub("_", result.msg_id) + ".xml"


__all__ = [
    "ACCEPTED",
    "PAIRING",
    "PENDING",
    "REASON_CODES",
    "REJECTED",
    "STATUSES",
    "Decision",
    "OriginalMessage",
    "OriginalTransaction",
    "SimulationError",
    "SimulationResult",
    "build_pain002",
    "file_name",
    "read_original",
    "simulate",
]
