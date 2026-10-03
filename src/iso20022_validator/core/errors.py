from dataclasses import dataclass, field


@dataclass(frozen=True)
class ValidationError:
    """One problem found in a message."""

    message: str
    line: int | None = None
    path: str | None = None
    # "xml" (not well-formed), "namespace" (unsupported), "schema" (XSD) or "business" (business rule)
    kind: str = "schema"
    rule: str | None = None  # which business rule, e.g. "ctrl_sum"; only set for kind == "business"

    @property
    def tag(self) -> str:
        """Short label separating the layers, e.g. "schema" or "business/ctrl_sum"."""
        return f"{self.kind}/{self.rule}" if self.rule else self.kind

    def __str__(self) -> str:
        loc = f"line {self.line}" if self.line else "line ?"
        path = f" {self.path}" if self.path else ""
        return f"[{self.tag}] {loc}{path}: {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    errors: list[ValidationError] = field(default_factory=list)
    namespace: str | None = None  # root namespace, if one was found
    schema_version: str | None = None  # e.g. "pain.001.001.03"; set when a schema was used
    business_checked: bool = False  # business rules only run on a schema-valid message
    business_rules_apply: bool = True  # False for message types that have no business rules (pain.002)

    @property
    def valid(self) -> bool:
        """True only if the message passed every layer that applies (never just the XSD)."""
        return not self.errors

    def format(self) -> str:
        lines = [f"Schema: {self.schema_version}"] if self.schema_version else []
        if self.valid:
            lines.append("VALID")
        else:
            lines.append(f"INVALID ({len(self.errors)} error{'s' if len(self.errors) != 1 else ''})")
            lines += [f"  {e}" for e in self.errors]
            if self.schema_version and self.business_rules_apply and not self.business_checked:
                lines.append("Business rules: not checked (fix the schema errors first)")
        return "\n".join(lines)
