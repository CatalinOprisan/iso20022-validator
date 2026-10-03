"""Real-browser checks of what AppTest cannot see: the Ace editor jumping to a clicked error's line, and the
full error text being readable on a narrow screen.

Starts the app on a free port and drives it with Playwright in an installed Edge/Chrome. Skipped when
Playwright or such a browser is missing (pip install playwright; no browser download is needed on Windows).
"""

import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from iso20022_validator import validate_file

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "src" / "iso20022_validator" / "app.py"
SAMPLE = ROOT / "samples" / "pain.001" / "pain.001.001.09" / "invalid_iban_checksum.xml"
UPLOAD = ROOT / "samples" / "pain.001" / "pain.001.001.09" / "invalid_unknown_element.xml"

CURSOR = """el => { const e = el.closest('.ace_editor').env.editor, row = e.getCursorPosition().row;
  return {row: row + 1, first: e.getFirstVisibleRow() + 1, last: e.getLastVisibleRow() + 1, text: e.session.getLine(row).trim()}; }"""
CONTENT = "el => el.closest('.ace_editor').env.editor.getValue()"


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(APP), "--server.port", str(port), "--server.headless", "true",
         "--browser.gatherUsageStats", "false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://localhost:{port}"
    try:
        for _ in range(60):
            try:
                if urllib.request.urlopen(f"{url}/_stcore/health", timeout=1).read() == b"ok":
                    break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.skip("the app did not start")
        yield url
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        for channel in ("msedge", "chrome", None):
            try:
                b = p.chromium.launch(channel=channel, headless=True) if channel else p.chromium.launch(headless=True)
                break
            except Exception:
                continue
        else:
            pytest.skip("no Chromium-based browser available")
        yield b
        b.close()


def open_app(browser, url, width=1200):
    page = browser.new_page(viewport={"width": width, "height": 1400})
    page.goto(url)
    page.wait_for_selector("text=ISO 20022 Validator", timeout=20000)
    return page


def open_test_mode(page):
    page.get_by_text("Test", exact=True).first.click()
    frame = page.frame_locator("iframe").first
    frame.locator(".ace_gutter").first.wait_for(timeout=15000)
    return frame


def put_text(page, frame, text):
    frame.locator(".ace_text-input").first.focus()
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(text)  # like a paste: no auto-indent or auto-closing tags
    time.sleep(1.2)


def validate(page):
    page.get_by_role("button", name="Validate").click()
    page.wait_for_selector(".iso-err", timeout=10000)


def test_clicking_an_error_moves_the_editor_to_its_line_and_shows_it(browser, base_url):
    error = validate_file(SAMPLE).errors[0]
    page = open_app(browser, base_url)
    frame = open_test_mode(page)
    put_text(page, frame, SAMPLE.read_text(encoding="utf-8"))
    validate(page)

    content = frame.locator(".ace_content").first
    before = content.evaluate(CURSOR)
    assert before["row"] != error.line  # the editor is elsewhere (the paste left it at the end)

    page.get_by_role("button", name=f"Line {error.line}").click()
    time.sleep(1.5)
    after = content.evaluate(CURSOR)
    assert after["row"] == error.line  # the cursor is on the line from the table ...
    assert after["first"] <= error.line <= after["last"]  # ... and that line is on screen
    assert "<IBAN>" in after["text"]

    # typing afterwards must not drag the editor back to the error line
    frame.locator(".ace_text-input").first.focus()
    page.keyboard.press("Control+End")
    page.keyboard.type("x", delay=30)
    time.sleep(1.2)
    assert content.evaluate(CURSOR)["row"] > error.line
    page.close()


@pytest.mark.parametrize("width", [1200, 620, 380])
def test_the_full_error_text_is_readable_without_resizing(browser, base_url, width):
    error = validate_file(SAMPLE).errors[0]
    page = open_app(browser, base_url, width)
    frame = open_test_mode(page)
    put_text(page, frame, SAMPLE.read_text(encoding="utf-8"))
    validate(page)

    message = page.locator(".iso-err-msg").first
    assert re.sub(r"\s+", " ", message.inner_text()).strip() == error.message  # all of it is in the page
    box = message.evaluate("e => { const r = e.getBoundingClientRect(); return {right: r.right, clipped: e.scrollWidth > e.clientWidth + 1}; }")
    assert box["right"] <= width  # inside the window, no sideways scrolling needed
    assert not box["clipped"]  # and nothing is cut off
    block = page.locator(".iso-err").first.evaluate("e => e.getBoundingClientRect().right")
    assert block <= width
    page.close()


def test_upload_mode_line_button_opens_the_file_in_the_editor_at_that_line(browser, base_url):
    error = validate_file(UPLOAD).errors[0]
    page = open_app(browser, base_url)
    page.set_input_files("input[type=file]", str(UPLOAD))
    page.wait_for_selector(".iso-err", timeout=10000)
    assert re.sub(r"\s+", " ", page.locator(".iso-err-msg").first.inner_text()).strip() == error.message

    page.get_by_role("button", name=f"Line {error.line}").click()
    frame = page.frame_locator("iframe").first
    frame.locator(".ace_gutter").first.wait_for(timeout=15000)
    time.sleep(2)
    content = frame.locator(".ace_content").first
    assert content.evaluate(CONTENT) == UPLOAD.read_text(encoding="utf-8")
    cursor = content.evaluate(CURSOR)
    assert cursor["row"] == error.line and cursor["first"] <= error.line <= cursor["last"]
    page.close()


def test_the_browser_tab_and_the_header_show_the_new_name(browser, base_url):
    page = open_app(browser, base_url)
    assert page.title() == "ISO 20022 Validator & Simulator"  # st.set_page_config, i.e. the tab
    assert page.get_by_role("heading", name="ISO 20022 Validator & Simulator").count() == 1
    assert page.locator("[data-testid=stException]").count() == 0  # and the page started without an import error
    page.close()
