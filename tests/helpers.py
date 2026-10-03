"""Shared by the UI tests: read the errors the app shows (one `.iso-err` block each) back out of an AppTest."""

import lxml.html
from streamlit.testing.v1 import AppTest


def errors_shown(at: AppTest) -> list[dict]:
    """The error blocks on screen as rows: Type, Rule, Line (int or None), XML path, Error (the full message)."""
    rows = []
    for el in at.get("html"):
        if "iso-err" not in el.proto.body:
            continue
        block = lxml.html.fragment_fromstring(el.proto.body)
        message = block.find_class("iso-err-msg")[0].text_content()
        line = block.get("data-line")
        rows.append(
            {
                "Type": block.get("data-kind"),
                "Rule": block.get("data-rule"),
                "Line": int(line) if line else None,
                "XML path": block.get("data-path") or None,
                "Error": message,
            }
        )
    return rows


def goto_buttons(at: AppTest) -> list:
    """The 'Line N' buttons of the error list, in order."""
    return [b for b in at.button if "_goto_" in (b.key or "")]
