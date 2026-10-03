"""Business rules for pain.001 (.03 and .09): are the values consistent and correct?

These run on a message that is already schema-valid (see engine.validate_bytes), so the elements
they read are known to exist and to have the right type. A rule is a function
`(Context) -> list[ValidationError]`; to add one, write it and append a `Rule` to RULES.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from lxml import etree

from .errors import ValidationError

# CtrlSum may differ from the sum of the amounts by less than half a cent (decimal rounding).
CTRL_SUM_TOLERANCE = Decimal("0.005")


@dataclass(frozen=True)
class Rule:
    name: str  # goes into ValidationError.rule
    description: str
    check: Callable[["Context"], list[ValidationError]]


class Context:
    """The message plus the helpers every rule needs."""

    def __init__(self, root: etree._Element, today: date):
        self.root = root
        self.today = today
        self.ns = {"p": etree.QName(root).namespace}
        self.group_header = root.find("p:CstmrCdtTrfInitn/p:GrpHdr", self.ns)
        self.payments = root.findall("p:CstmrCdtTrfInitn/p:PmtInf", self.ns)

    def transactions(self, scope: etree._Element | None = None) -> list[etree._Element]:
        """CdtTrfTxInf elements of one PmtInf, or of the whole message when scope is None."""
        if scope is None:
            return self.root.xpath("//p:CdtTrfTxInf", namespaces=self.ns)
        return scope.findall("p:CdtTrfTxInf", self.ns)

    def amount(self, tx: etree._Element) -> Decimal | None:
        el = tx.find("p:Amt/p:InstdAmt", self.ns)
        if el is None:
            el = tx.find("p:Amt/p:EqvtAmt/p:Amt", self.ns)
        return Decimal(el.text) if el is not None and el.text else None

    def scopes(self):
        """(name for messages, element holding CtrlSum/NbOfTxs, its transactions): GrpHdr, then each PmtInf."""
        if self.group_header is not None:
            yield "the message", self.group_header, self.transactions()
        for pmt in self.payments:
            yield "this PmtInf", pmt, self.transactions(pmt)


def _path(el: etree._Element) -> str:
    """/Document/CstmrCdtTrfInitn/PmtInf[2]/CtrlSum: like the schema paths, plus [n] among same-named siblings."""
    parts = []
    while el is not None:
        name = etree.QName(el).localname
        parent = el.getparent()
        if parent is not None:
            same = [s for s in parent if s.tag == el.tag]
            if len(same) > 1:
                name += f"[{same.index(el) + 1}]"
        parts.append(name)
        el = parent
    return "/" + "/".join(reversed(parts))


def _error(rule: str, el: etree._Element, message: str) -> ValidationError:
    return ValidationError(message, line=el.sourceline, path=_path(el), kind="business", rule=rule)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- rules ---------------------------------------------------------------------------------


def check_ctrl_sum(ctx: Context) -> list[ValidationError]:
    """CtrlSum (GrpHdr and each PmtInf) must equal the sum of the amounts of its transactions."""
    errors = []
    for _scope, holder, txs in ctx.scopes():
        sum_el = holder.find("p:CtrlSum", ctx.ns)
        amounts = [ctx.amount(tx) for tx in txs]
        if sum_el is None or any(a is None for a in amounts):
            continue
        declared, total = Decimal(sum_el.text), sum(amounts, Decimal(0))
        if abs(declared - total) >= CTRL_SUM_TOLERANCE:
            errors.append(
                _error("ctrl_sum", sum_el, f"CtrlSum is {sum_el.text.strip()} but transactions sum to {total:f}")
            )
    return errors


def check_nb_of_txs(ctx: Context) -> list[ValidationError]:
    """NbOfTxs (GrpHdr and each PmtInf) must equal the number of CdtTrfTxInf elements it covers."""
    errors = []
    for scope, holder, txs in ctx.scopes():
        count_el = holder.find("p:NbOfTxs", ctx.ns)
        if count_el is None:
            continue
        declared = int(count_el.text)
        if declared != len(txs):
            errors.append(
                _error(
                    "nb_of_txs",
                    count_el,
                    f"NbOfTxs is {declared} but {scope} contains {_plural(len(txs), 'transaction')}",
                )
            )
    return errors


def iban_mod97_ok(iban: str) -> bool:
    """ISO 13616 checksum: move the first four characters to the end, letters become 10..35, number mod 97 == 1."""
    rearranged = (iban[4:] + iban[:4]).upper()
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def iban_check_digits(iban: str) -> str:
    """The two check digits this country code and account number would need."""
    rearranged = (iban[4:] + iban[:2] + "00").upper()
    return f"{98 - int(''.join(str(int(c, 36)) for c in rearranged)) % 97:02d}"


def check_iban_checksum(ctx: Context) -> list[ValidationError]:
    """Every IBAN in the message must pass the mod-97 checksum (the XSD only checks its format)."""
    errors = []
    for el in ctx.root.xpath("//p:IBAN", namespaces=ctx.ns):
        iban = (el.text or "").strip()
        if not iban_mod97_ok(iban):
            errors.append(
                _error(
                    "iban_checksum",
                    el,
                    f"IBAN {iban} fails the mod-97 checksum "
                    f"(for this country code and account number the check digits would be {iban_check_digits(iban)})",
                )
            )
    return errors


def check_execution_date(ctx: Context) -> list[ValidationError]:
    """ReqdExctnDt of each PmtInf must not be before today."""
    errors = []
    for pmt in ctx.payments:
        holder = pmt.find("p:ReqdExctnDt", ctx.ns)
        if holder is None:
            continue
        el = holder  # .03: the date is the element's text
        if len(holder):  # .09: <Dt> (date) or <DtTm> (date and time)
            el = holder.find("p:Dt", ctx.ns)
            if el is None:
                el = holder.find("p:DtTm", ctx.ns)
        value = (el.text or "").strip()
        if date.fromisoformat(value[:10]) < ctx.today:
            errors.append(_error("execution_date_past", el, f"ReqdExctnDt {value} is in the past (today is {ctx.today})"))
    return errors


RULES: list[Rule] = [
    Rule("ctrl_sum", "CtrlSum equals the sum of the transaction amounts", check_ctrl_sum),
    Rule("nb_of_txs", "NbOfTxs equals the number of transactions", check_nb_of_txs),
    Rule("iban_checksum", "IBANs pass the mod-97 checksum", check_iban_checksum),
    Rule("execution_date_past", "ReqdExctnDt is not in the past", check_execution_date),
]


def check_business_rules(root: etree._Element, today: date) -> list[ValidationError]:
    """All rule violations of a schema-valid message, in document order."""
    ctx = Context(root, today)
    errors = [e for rule in RULES for e in rule.check(ctx)]
    errors.sort(key=lambda e: e.line or 0)
    return errors
