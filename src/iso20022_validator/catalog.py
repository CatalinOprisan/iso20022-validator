"""What the app can show: categories -> message types -> modes. No Streamlit in here.

This is the one place to touch to add a message type to the navigation:

  1. put its schema(s) in messages/<area>/<number>_<..>/schema.xsd (the engine discovers them), and
  2. give its category a `MessageType` below, with the modes that make sense for it.

A category without message types is "coming soon": the sidebar leaves it out of the dropdown and lists it by name.
The catalog only describes the navigation; validation itself does not depend on it.
"""

from dataclasses import dataclass

from .core.engine import supported_namespaces, version_label

UPLOAD, TEST, GENERATE, BATCH, SIMULATE = "Upload file", "Test", "Generate", "Batch Generate", "Simulate"
ALL_MODES = (UPLOAD, TEST, GENERATE, BATCH, SIMULATE)


@dataclass(frozen=True)
class MessageType:
    """One message, whatever its schema versions: e.g. pain.001 has the schemas pain.001.001.03 and .09."""

    family: str  # area + message number, the start of the schema identifier: "pain.001"
    title: str
    modes: tuple[str, ...]  # the tabs that make sense for this message, in display order

    @property
    def label(self) -> str:
        return f"{self.family} · {self.title}"

    @property
    def schemas(self) -> list[str]:
        """The installed schema versions of this message, e.g. ['pain.001.001.03', 'pain.001.001.09']."""
        versions = (version_label(ns) for ns in supported_namespaces())
        return sorted(v for v in versions if family_of(v) == self.family)


@dataclass(frozen=True)
class Category:
    area: str  # the folder under messages/ and the first part of the schema identifier: "pain"
    name: str
    types: tuple[MessageType, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.types)


CATALOG: tuple[Category, ...] = (
    Category(
        "pain",
        "Payments Initiation",
        (
            MessageType("pain.001", "Customer Credit Transfer Initiation", ALL_MODES),
            MessageType("pain.002", "Customer Payment Status Report", (UPLOAD, TEST)),
        ),
    ),
    Category("pacs", "Clearing & Settlement"),
    Category("camt", "Cash Management"),
    Category("admi", "Administration"),
)


def family_of(schema_version: str) -> str:
    """'pain.001.001.09' -> 'pain.001'."""
    return ".".join(schema_version.split(".")[:2])


def available_categories() -> list[Category]:
    return [c for c in CATALOG if c.available]


def coming_soon() -> list[Category]:
    return [c for c in CATALOG if not c.available]


def find(schema_version: str) -> tuple[Category, MessageType] | None:
    """The category and message type a schema version belongs to, or None if the catalog does not know it."""
    family = family_of(schema_version)
    for category in CATALOG:
        for message_type in category.types:
            if message_type.family == family:
                return category, message_type
    return None
