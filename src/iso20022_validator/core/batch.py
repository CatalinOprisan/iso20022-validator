"""Batch test-suite generation: many pain.001 messages (valid and deliberately invalid) in one zip.

Valid files vary the amount and the MsgId/EndToEndId of a template. Invalid files are valid files
with exactly one injected error, chosen from ERROR_TYPES. Every file is run back through the
validator before it is packaged, so the manifest never describes a file wrongly.

To add an error type: write a function `(root, elements, rng) -> str` that breaks the message in
exactly one way and returns a short description, and append `ErrorType(name, description, fn)`
to ERROR_TYPES. Nothing else needs to change.
"""

import csv
import io
import random
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

from .engine import validate_bytes
from .errors import ValidationError
from .generator import MessageFields, TemplateDocument, new_end_to_end_id, new_msg_id

MAX_FILES = 100
MANIFEST_NAME = "manifest.csv"
MANIFEST_COLUMNS = ("filename", "status", "error_type", "detail", "schema_version")

Elements = dict[str, etree._Element]


# ---- error types ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorType:
    name: str  # goes into the manifest and the file name; keep it a short snake_case word
    description: str
    inject: Callable[[etree._Element, Elements, random.Random], str]


def _inject_invalid_iban(root, els, rng) -> str:
    key = rng.choice(["debtor_iban", "creditor_iban"])
    el = els[key]
    iban = el.text
    variant = rng.choice(["letters", "lowercase", "spaces"])
    if variant == "letters":  # check digits that are not digits
        el.text = iban[:2] + rng.choice(["XX", "9Z", "A1"]) + iban[4:]
        how = "letters in the check digits"
    elif variant == "lowercase":
        el.text = iban.lower()
        how = "lowercase country code"
    else:
        el.text = " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
        how = "spaces inside the IBAN"
    return f"{key.replace('_', ' ')}: {how} ({el.text})"


def _inject_invalid_amount(root, els, rng) -> str:
    el = els["amount"]
    variant = rng.choice(["negative", "text", "comma"])
    if variant == "negative":
        el.text = "-" + el.text.lstrip("-")
    elif variant == "text":
        el.text = rng.choice(["ABC", "one hundred", "N/A"])
    else:
        el.text = el.text.replace(".", ",")
    return f"amount {variant}: {el.text}"


def _inject_missing_field(root, els, rng) -> str:
    key = rng.choice(["msg_id", "end_to_end_id"])
    el = els[key]
    parent, prev = el.getparent(), el.getprevious()
    if prev is not None:  # keep the indentation tidy: the removed element's tail moves to its predecessor
        prev.tail = el.tail
    else:
        parent.text = el.tail
    parent.remove(el)
    return f"{etree.QName(el).localname} removed"


def _inject_malformed_date(root, els, rng) -> str:
    el = els["execution_date"]
    el.text = rng.choice(["05/10/2026", "20261005", "2026-10-5", "2026-13-01", "2026-02-30"])
    return f"execution date: {el.text}"


ERROR_TYPES: list[ErrorType] = [
    ErrorType("invalid_iban", "IBAN with malformed check digits or characters", _inject_invalid_iban),
    ErrorType("invalid_amount", "negative or non-numeric amount", _inject_invalid_amount),
    ErrorType("missing_field", "mandatory MsgId or EndToEndId dropped", _inject_missing_field),
    ErrorType("malformed_date", "wrong date format or impossible date", _inject_malformed_date),
]


# ---- result types --------------------------------------------------------------------------


class BatchError(Exception):
    """The batch cannot be generated (e.g. the base values do not make a valid message)."""

    def __init__(self, message: str, errors: list[ValidationError] | None = None):
        super().__init__(message)
        self.errors = errors or []


@dataclass(frozen=True)
class BatchFile:
    filename: str
    valid: bool
    error_type: str | None  # None for valid files
    detail: str
    xml: bytes

    @property
    def status(self) -> str:
        return "valid" if self.valid else "invalid"

    def manifest_row(self, schema_version: str) -> dict[str, str]:
        return {
            "filename": self.filename,
            "status": self.status,
            "error_type": self.error_type or "",
            "detail": self.detail,
            "schema_version": schema_version,
        }


@dataclass(frozen=True)
class BatchResult:
    files: list[BatchFile]
    zip_bytes: bytes
    schema_version: str

    @property
    def manifest(self) -> list[dict[str, str]]:
        return [f.manifest_row(self.schema_version) for f in self.files]


# ---- generation ----------------------------------------------------------------------------


def invalid_count(count: int, valid_percent: float) -> int:
    """How many of `count` files are invalid: the share rounded half up (never banker's rounding)."""
    return int(Decimal(count) * Decimal(str(100 - valid_percent)) / 100 + Decimal("0.5"))


def _varied_amount(base: Decimal, rng: random.Random) -> str:
    amount = (base * Decimal(str(round(rng.uniform(0.1, 2.0), 4)))).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return f"{max(amount, Decimal('0.01')):.2f}"


def _unique(make: Callable[[], str], used: set[str]) -> str:
    while (value := make()) in used:
        pass
    used.add(value)
    return value


def _manifest_csv(files: Sequence[BatchFile], schema_version: str) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=MANIFEST_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(f.manifest_row(schema_version) for f in files)
    return out.getvalue().encode("utf-8")


def generate_batch(
    template: bytes,
    base: MessageFields | None = None,
    *,
    count: int = 10,
    valid_percent: float = 80,
    seed: int | None = None,
    error_types: Sequence[ErrorType] | None = None,
    today: date | None = None,
) -> BatchResult:
    """Generate `count` messages from a template, `valid_percent` of them valid, packaged as a zip.

    base: field values to use (default: the template's own). MsgId/EndToEndId in it are ignored,
    every file gets fresh ones. The amount is the centre of the variation (0.1x to 2x, 2 decimals).
    seed: makes amounts, positions and error choices reproducible (IDs are always fresh).
    today: the date the execution-date business rule compares against (default: the real date).

    Valid files pass the schema AND the business rules (CtrlSum is recomputed per file); invalid files
    are broken at schema level, so each has exactly one error.
    """
    if not 1 <= count <= MAX_FILES:
        raise ValueError(f"count must be between 1 and {MAX_FILES}")
    if not 0 <= valid_percent <= 100:
        raise ValueError("valid_percent must be between 0 and 100")
    types = list(error_types if error_types is not None else ERROR_TYPES)
    if not types and invalid_count(count, valid_percent):
        raise ValueError("no error types available for the invalid files")

    doc = TemplateDocument(template)  # TemplateError if the template is unusable
    base = base or doc.fields
    probe = validate_bytes(doc.render(base, sync_control_sums=True), today=today)
    if not probe.valid:
        raise BatchError("The field values do not produce a valid message.", probe.errors)
    base_amount = Decimal(base.amount)

    rng = random.Random(seed)
    n_invalid = invalid_count(count, valid_percent)
    invalid_slots = set(rng.sample(range(count), n_invalid))
    bag: list[ErrorType] = []  # shuffled bag: random order, but all types get used before any repeats

    used_ids: set[str] = set()
    files: list[BatchFile] = []
    for i in range(count):
        values = replace(
            base,
            msg_id=_unique(new_msg_id, used_ids),
            end_to_end_id=_unique(new_end_to_end_id, used_ids),
            amount=_varied_amount(base_amount, rng),
        )
        if i not in invalid_slots:
            xml = doc.render(values, sync_control_sums=True)
            check = validate_bytes(xml, today=today)
            if not check.valid:  # cannot happen if the base probe passed; never ship a wrong label
                raise BatchError("A generated valid message failed validation.", check.errors)
            files.append(BatchFile(f"{i + 1:03d}_valid.xml", True, None, "", xml))
            continue

        for _attempt in range(10):
            if not bag:
                bag = rng.sample(types, len(types))
            error = bag.pop()
            details: list[str] = []
            xml = doc.render(
                values,
                sync_control_sums=True,
                mutate=lambda root, els, error=error, details=details: details.append(error.inject(root, els, rng)),
            )
            if not validate_bytes(xml, today=today).valid:
                files.append(BatchFile(f"{i + 1:03d}_{error.name}.xml", False, error.name, details[0], xml))
                break
        else:
            raise BatchError("Could not produce an invalid message; check the error types.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.writestr(f.filename, f.xml)
        zf.writestr(MANIFEST_NAME, _manifest_csv(files, doc.schema_version))
    return BatchResult(files, buffer.getvalue(), doc.schema_version)


__all__ = [
    "ERROR_TYPES",
    "MANIFEST_COLUMNS",
    "MANIFEST_NAME",
    "MAX_FILES",
    "BatchError",
    "BatchFile",
    "BatchResult",
    "ErrorType",
    "generate_batch",
    "invalid_count",
]
