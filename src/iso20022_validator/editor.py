"""The XML code editor of the Test mode: Ace (via streamlit-ace) with line numbers and XML highlighting.

Kept in its own module so the rest of the app depends on one small function, and tests can swap
it for a plain text box (a custom component cannot be driven by Streamlit's AppTest).
"""

import time
from collections.abc import Iterable

import streamlit.components.v1 as components
from streamlit_ace import st_ace


def annotations_for(errors: Iterable) -> list[dict]:
    """Ace gutter markers: one per error that has a line. Ace rows are 0-based, our lines are 1-based."""
    return [
        {"row": e.line - 1, "column": 0, "type": "error", "text": f"[{e.tag}] {e.message}"}
        for e in errors
        if e.line
    ]


def xml_editor(value: str, key: str, annotations: list[dict] | None = None) -> str:
    """Show the editor and return its current text.

    auto_update=True keeps the returned text in step with what is typed, so a Validate click always
    checks what is on screen. That only updates the text; nothing is validated until Validate is clicked.
    """
    return st_ace(
        value=value,
        language="xml",
        theme="chrome",
        keybinding="vscode",
        height=480,
        font_size=13,
        tab_size=2,
        show_gutter=True,  # line numbers
        show_print_margin=False,
        wrap=False,
        annotations=annotations or [],
        auto_update=True,
        key=key,
    )


# streamlit-ace has no "go to line" argument, so a one-off script finds the live Ace instance in the
# page (the editor and this script are both same-origin iframes) and drives it through Ace's own API.
_GOTO_SCRIPT = """
<script>
(function () {
  var line = %(line)d, started = Date.now();  // run id: %(run)d
  function findEditor() {
    var frames = window.parent.document.querySelectorAll("iframe");
    for (var i = 0; i < frames.length; i++) {
      try {
        var el = frames[i].contentDocument && frames[i].contentDocument.querySelector(".ace_editor");
        if (el && el.env && el.env.editor) return { frame: frames[i], editor: el.env.editor, win: frames[i].contentWindow };
      } catch (e) {}
    }
    return null;
  }
  function go() {
    var found = findEditor();
    if (!found) { if (Date.now() - started < 4000) setTimeout(go, 100); return; }
    var ed = found.editor, row = Math.max(0, Math.min(line, ed.session.getLength()) - 1);
    ed.gotoLine(row + 1, 0, false);                        // cursor to the start of the line
    ed.renderer.scrollCursorIntoView({ row: row, column: 0 }, 0.5);  // and scroll it to mid-height
    try {                                                   // flash the line so the eye finds it
      var Range = found.win.ace.require("ace/range").Range;
      var id = ed.session.addMarker(new Range(row, 0, row, 1), "ace_selection", "fullLine");
      setTimeout(function () { ed.session.removeMarker(id); }, 1500);
    } catch (e) {}
    ed.focus();
    found.frame.scrollIntoView({ block: "nearest", behavior: "smooth" });  // make sure the editor is on screen
  }
  go();
})();
</script>
"""


def scroll_to_line(line: int) -> None:
    """Move the editor's cursor to `line` (1-based, like the error table), scroll it into view and flash it.

    Call it for ONE run only: the script runs whenever this element is rendered, so rendering it
    again on every rerun would pull the editor back to that line while the user types.
    """
    components.html(_GOTO_SCRIPT % {"line": line, "run": time.time_ns()}, height=0)
