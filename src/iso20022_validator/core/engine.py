"""Validation engine.

Layer 1, schema: message types are plugins (messages/<id>/schema.xsd). The schema is chosen from
the namespace of the root element, matched against each XSD's targetNamespace.
Layer 2, business rules (rules.py): run only on a schema-valid message.
A message is valid only if it passes every layer that applies.
"""

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from xml.parsers import expat

import xmlschema
from lxml import etree

from .errors import ValidationError, ValidationResult
from .rules import check_business_rules

MESSAGES_DIR = Path(__file__).resolve().parent.parent / "messages"
NS_PREFIX = "urn:iso:std:iso:20022:tech:xsd:"

_NS = re.compile(r"\{(?:urn|https?):[^}]*\}")  # {namespace} prefixes, not regex quantifiers like {2,2}


@lru_cache(maxsize=None)
def supported_namespaces() -> dict[str, Path]:
    """Map targetNamespace -> XSD path for every installed message plugin."""
    found = {}
    for xsd in sorted(MESSAGES_DIR.glob("*/schema.xsd")):
        ns = etree.parse(str(xsd)).getroot().get("targetNamespace")
        if ns:
            found[ns] = xsd
    return found


def version_label(namespace: str) -> str:
    """'urn:iso:std:iso:20022:tech:xsd:pain.001.001.03' -> 'pain.001.001.03'."""
    return namespace.removeprefix(NS_PREFIX)


@lru_cache(maxsize=None)
def _schema(xsd: Path) -> xmlschema.XMLSchema:
    return xmlschema.XMLSchema(str(xsd))


def _clean(reason: str) -> str:
    """Single-line message with the verbose {namespace} prefixes removed."""
    return _NS.sub("", " ".join(str(reason).split()))


def _stray_text_lines(data: bytes) -> dict[int, int]:
    """Document-order index of each element -> line of its first non-whitespace character data.

    The schema reports "character data between child elements" on the *parent*, whose start tag may
    be lines above the text; expat knows the line of every piece of text. {} if expat cannot read the
    document (e.g. an encoding it does not support), in which case the parent's line is kept.
    """
    stack: list[int] = []
    count = 0
    lines: dict[int, int] = {}

    def start(_name, _attrs):
        nonlocal count
        stack.append(count)
        count += 1

    def text(chunk):
        if stack and chunk.strip():
            lines.setdefault(stack[-1], parser.CurrentLineNumber)

    parser = expat.ParserCreate()
    parser.StartElementHandler = start
    parser.EndElementHandler = lambda _name: stack.pop()
    parser.CharacterDataHandler = text
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        return {}
    return lines


def _check_schema(data: bytes) -> tuple[ValidationResult, etree._Element | None]:
    """Layer 1. Also returns the parsed root when the message is schema-valid (for layer 2)."""
    try:
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError as exc:
        return ValidationResult([ValidationError(f"Not well-formed XML: {exc.msg}", line=exc.lineno, kind="xml")]), None

    namespace = etree.QName(root).namespace or ""
    xsd = supported_namespaces().get(namespace)
    if xsd is None:
        shown = f"'{namespace}'" if namespace else "(none)"
        supported = ", ".join(version_label(ns) for ns in supported_namespaces())
        error = ValidationError(
            f"Unsupported namespace {shown} on root element <{etree.QName(root).localname}>. Supported: {supported}",
            line=root.sourceline,
            path=f"/{etree.QName(root).localname}",
            kind="namespace",
        )
        return ValidationResult([error], namespace=namespace or None), None

    errors = []
    stray_text = None  # computed on demand: only character-data errors need it
    try:
        for err in _schema(xsd).iter_errors(root):
            message = _clean(err.reason or err.message)
            line = getattr(err.elem, "sourceline", None)
            if "character data" in message and err.elem is not None:
                if stray_text is None:
                    stray_text = _stray_text_lines(data)
                    order = {el: i for i, el in enumerate(root.iter(etree.Element))}
                line = stray_text.get(order.get(err.elem), line)  # the text's own line, not its parent's
            errors.append(ValidationError(message=message, line=line, path=err.path))
    except xmlschema.XMLSchemaException as exc:
        errors.append(ValidationError(_clean(str(exc)), kind="xml"))
    errors.sort(key=lambda e: (e.line or 0))
    return ValidationResult(errors, namespace, version_label(namespace)), (None if errors else root)


def validate_schema(data: bytes) -> ValidationResult:
    """Layer 1 only: is the XML well-formed, in a supported namespace, and valid against the XSD?"""
    return _check_schema(data)[0]


def validate_bytes(data: bytes, *, today: date | None = None) -> ValidationResult:
    """Schema validation, then the business rules if (and only if) the schema passes.

    today: the date the execution-date rule compares against (default: the real date).
    """
    result, root = _check_schema(data)
    if root is None:
        return result
    errors = check_business_rules(root, today or date.today())
    return ValidationResult(errors, result.namespace, result.schema_version, business_checked=True)


def validate_file(path: str | Path, *, today: date | None = None) -> ValidationResult:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return ValidationResult([ValidationError(f"Cannot read file: {exc.strerror or exc}", kind="xml")])
    return validate_bytes(data, today=today)
