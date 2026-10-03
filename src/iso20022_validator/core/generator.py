"""Generate a new pain.001 message from a valid template by replacing a few key fields.

Only the text of the listed fields is changed; the rest of the document is serialised back
as it was. The result is run through the validator (schema and business rules), and no XML is returned unless it is valid.

`TemplateDocument` is the reusable building block: parse a template once, render it many
times with different values (the batch generator also uses it to inject deliberate errors).
"""

import re
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from lxml import etree

from .engine import validate_bytes, validate_schema
from .errors import ValidationError

_ALNUM = string.ascii_uppercase + string.digits
_XML_DECL = re.compile(rb"^\s*<\?xml[^>]*\?>[ \t]*(?:\r?\n)?")


class TemplateError(Exception):
    """The template cannot be used (invalid, or lacks one of the editable fields)."""

    def __init__(self, message: str, errors: list[ValidationError] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class MessageFields:
    """The editable fields. All values are plain strings, exactly as they appear in the XML."""

    msg_id: str
    debtor_name: str
    debtor_iban: str
    creditor_name: str
    creditor_iban: str
    amount: str
    currency: str
    execution_date: str
    end_to_end_id: str


@dataclass(frozen=True)
class Template:
    fields: MessageFields
    schema_version: str
    transaction_count: int  # CdtTrfTxInf elements in the whole message; only the first is editable


@dataclass(frozen=True)
class GenerationResult:
    xml: bytes | None  # None whenever the edited message is not valid
    errors: list[ValidationError]
    schema_version: str | None

    @property
    def ok(self) -> bool:
        return self.xml is not None


def generate_id(prefix: str, now: datetime | None = None) -> str:
    """e.g. MSG-20261003-103555-K3X9 (<= 35 chars, the Max35Text limit)."""
    now = now or datetime.now()
    suffix = "".join(secrets.choice(_ALNUM) for _ in range(4))
    return f"{prefix}-{now:%Y%m%d-%H%M%S}-{suffix}"


def new_msg_id(now: datetime | None = None) -> str:
    return generate_id("MSG", now)


def new_end_to_end_id(now: datetime | None = None) -> str:
    return generate_id("E2E", now)


# Paths relative to <Document>; "p" is bound to the message namespace.
_PMT = "p:CstmrCdtTrfInitn/p:PmtInf[1]"
_TX = f"{_PMT}/p:CdtTrfTxInf[1]"
_PATHS = {
    "msg_id": "p:CstmrCdtTrfInitn/p:GrpHdr/p:MsgId",
    "debtor_name": f"{_PMT}/p:Dbtr/p:Nm",
    "debtor_iban": f"{_PMT}/p:DbtrAcct/p:Id/p:IBAN",
    "creditor_name": f"{_TX}/p:Cdtr/p:Nm",
    "creditor_iban": f"{_TX}/p:CdtrAcct/p:Id/p:IBAN",
    "amount": f"{_TX}/p:Amt/p:InstdAmt",
    "currency": f"{_TX}/p:Amt/p:InstdAmt",  # attribute Ccy
    "execution_date": f"{_PMT}/p:ReqdExctnDt",  # text in .03, <Dt> child in .09
    "end_to_end_id": f"{_TX}/p:PmtId/p:EndToEndId",
}
_LABELS = {
    "msg_id": "Message ID (MsgId)",
    "debtor_name": "debtor name",
    "debtor_iban": "debtor IBAN",
    "creditor_name": "creditor name",
    "creditor_iban": "creditor IBAN",
    "amount": "instructed amount (InstdAmt)",
    "currency": "currency (InstdAmt/@Ccy)",
    "execution_date": "requested execution date (single date)",
    "end_to_end_id": "EndToEndId",
}


def _parse(data: bytes) -> etree._ElementTree:
    return etree.parse(BytesIO(data), etree.XMLParser(resolve_entities=False, no_network=True))


def _locate(root: etree._Element) -> dict[str, etree._Element]:
    """The element holding each field's value, or TemplateError naming what is missing."""
    ns = {"p": etree.QName(root).namespace}
    found, missing = {}, []
    for name, path in _PATHS.items():
        hits = root.xpath(path, namespaces=ns)
        el = hits[0] if hits else None
        if name == "execution_date" and el is not None and len(el):
            el = el.find("p:Dt", ns)  # .09: DateAndDateTime2Choice -> <Dt>
        if el is None:
            missing.append(_LABELS[name])
        else:
            found[name] = el
    if missing:
        raise TemplateError("Template has no " + ", ".join(missing) + ".")
    return found


def _control_sums(root: etree._Element) -> list[tuple[etree._Element, Decimal]] | None:
    """Every <CtrlSum> in the message with the total of the amounts it covers.

    None when the totals cannot be computed (a transaction without InstdAmt, or an amount that is
    not a number: the schema check will report that, so CtrlSum is simply left alone).
    """
    ns = {"p": etree.QName(root).namespace}
    result = []
    for scope in root.xpath("p:CstmrCdtTrfInitn/p:GrpHdr | p:CstmrCdtTrfInitn/p:PmtInf", namespaces=ns):
        sum_el = scope.find("p:CtrlSum", ns)
        if sum_el is None:
            continue
        txs = root.xpath("//p:CdtTrfTxInf", namespaces=ns) if etree.QName(scope).localname == "GrpHdr" else scope.findall("p:CdtTrfTxInf", ns)
        amounts = [tx.find("p:Amt/p:InstdAmt", ns) for tx in txs]
        if any(a is None for a in amounts):
            return None
        try:
            result.append((sum_el, sum(Decimal(a.text) for a in amounts)))
        except InvalidOperation:
            return None
    return result


class TemplateDocument:
    """A schema-valid template that can be rendered repeatedly with different field values.

    Only the schema is required of a template: an old one whose execution date has passed or whose
    control sums are stale is still a usable starting point (the generated message must pass everything).
    """

    def __init__(self, data: bytes):
        check = validate_schema(data)
        if not check.valid:
            raise TemplateError("The template is not a valid pain.001 message.", check.errors)
        self._data = data
        tree = _parse(data)
        root = tree.getroot()
        els = _locate(root)
        values = {n: (el.text or "") for n, el in els.items()}
        values["currency"] = els["currency"].get("Ccy", "")
        ns = {"p": etree.QName(root).namespace}
        self.fields = MessageFields(**values)
        self.schema_version = check.schema_version
        self.transaction_count = len(root.xpath("//p:CdtTrfTxInf", namespaces=ns))
        self._encoding = tree.docinfo.encoding or "UTF-8"

    def render(
        self,
        values: MessageFields,
        *,
        sync_control_sums: bool = False,
        mutate: Callable[[etree._Element, dict[str, etree._Element]], None] | None = None,
    ) -> bytes:
        """The template with `values` in the editable fields. NOT validated.

        sync_control_sums: recompute every CtrlSum from the amounts, so an edited amount keeps them correct.
        mutate(root, elements): last-moment hook, used to inject deliberate errors.
        """
        tree = _parse(self._data)
        root = tree.getroot()
        els = _locate(root)
        for f in fields(MessageFields):
            if f.name == "currency":
                els["currency"].set("Ccy", values.currency)
            else:
                els[f.name].text = getattr(values, f.name)

        if sync_control_sums:
            for sum_el, total in _control_sums(root) or []:
                sum_el.text = format(total, "f")
        if mutate:
            mutate(root, els)

        body = etree.tostring(tree, encoding=self._encoding, xml_declaration=False)
        decl = _XML_DECL.match(self._data)
        out = (decl.group(0) if decl else b"") + body
        if self._data.endswith((b"\n", b"\r\n")) and not out.endswith(b"\n"):
            out += b"\r\n" if self._data.endswith(b"\r\n") else b"\n"
        return out


def read_template(data: bytes) -> Template:
    """Validate the template and read its editable fields."""
    doc = TemplateDocument(data)
    return Template(doc.fields, doc.schema_version, doc.transaction_count)


def generate(template: bytes, values: MessageFields, *, today: date | None = None) -> GenerationResult:
    """Replace the template's editable fields with `values`; validate; return the new XML or errors.

    CtrlSum is recomputed from the amounts. The result must pass the schema AND the business rules
    (e.g. an IBAN with a wrong checksum, or an execution date in the past, is reported, not offered).
    """
    out = TemplateDocument(template).render(values, sync_control_sums=True)
    result = validate_bytes(out, today=today)
    if not result.valid:
        return GenerationResult(None, result.errors, result.schema_version)
    return GenerationResult(out, [], result.schema_version)


__all__ = [
    "GenerationResult",
    "MessageFields",
    "Template",
    "TemplateDocument",
    "TemplateError",
    "generate",
    "generate_id",
    "new_end_to_end_id",
    "new_msg_id",
    "read_template",
]
