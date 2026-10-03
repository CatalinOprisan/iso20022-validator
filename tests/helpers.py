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


# ---- navigation: sidebar (category / message type) and the tab bar of the selected message type ----


def select_tab(at: AppTest, mode: str, family: str = "pain.001") -> AppTest:
    """Open a tab like clicking it: the tab bar's state is its widget key, one tab bar per message type."""
    at.session_state[f"mode_{family}"] = mode
    return at.run()


def current_tab(at: AppTest, family: str = "pain.001") -> str:
    return at.session_state[f"mode_{family}"]


def tab_labels(at: AppTest) -> list[str]:
    return [t.label for t in at.tabs]


def select_message_type(at: AppTest, family: str, area: str = "pain") -> AppTest:
    at.sidebar.selectbox(key=f"nav_type_{area}").select(family)
    return at.run()


def detected_schema(at: AppTest) -> str | None:
    """The sidebar's 'Detected schema' value, None when it shows the dash."""
    values = [m.value for m in at.sidebar.markdown if m.value.startswith("**")]
    shown = values[-1].strip("*`") if values else None
    return None if shown in (None, "—") else shown
