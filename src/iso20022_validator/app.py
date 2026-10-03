import hashlib
import html
import re
from datetime import date

import streamlit as st

from iso20022_validator import editor
from iso20022_validator.core import (
    MAX_FILES,
    REASON_CODES,
    STATUSES,
    BatchError,
    Decision,
    MessageFields,
    SimulationError,
    TemplateError,
    generate,
    generate_batch,
    new_end_to_end_id,
    new_msg_id,
    read_original,
    read_template,
    simulate,
    validate_bytes,
)
from iso20022_validator.core.simulate import REJECTED, file_name

UPLOAD, TEST, GENERATE, BATCH, SIMULATE = "Upload file", "Test", "Generate", "Batch Generate", "Simulate"

_ENCODING_DECL = re.compile(r'^(<\?xml[^>]*?\bencoding\s*=\s*)(["\'])[^"\']*\2', re.IGNORECASE)
_PLAIN_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def pasted_to_bytes(text: str) -> bytes:
    """Encode pasted text as UTF-8; a declared encoding is rewritten to match, so the parser agrees."""
    text = _ENCODING_DECL.sub(r'\1"UTF-8"', text)
    return text.encode("utf-8")


_KIND_COLORS = {"schema": "#cf222e", "business": "#bf8700", "xml": "#8250df", "namespace": "#8250df"}


def _error_html(e) -> str:
    """One error as a block: the full message wraps instead of being cut off. data-* attributes carry the fields."""
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)  # noqa: E731
    color = _KIND_COLORS.get(e.kind, "#57606a")
    tag = e.kind + (f" · {e.rule}" if e.rule else "")
    return (
        f'<div class="iso-err" data-kind="{esc(e.kind)}" data-rule="{esc(e.rule)}" data-line="{esc(e.line)}" '
        f'data-path="{esc(e.path)}" style="border-left:4px solid {color};padding:2px 0 2px 10px;line-height:1.45">'
        f'<div style="font-size:12px;opacity:.75;overflow-wrap:anywhere"><b style="color:{color}">{esc(tag)}</b>'
        f'{" · " + esc(e.path) if e.path else ""}</div>'
        f'<div class="iso-err-msg" style="overflow-wrap:anywhere;white-space:pre-wrap">{esc(e.message)}</div></div>'
    )


def error_list(errors, key: str, goto=None) -> None:
    """Every error as its own readable block (type, rule, path, the whole message).

    goto(line): when given, the line number is a button that calls it; used where an editor shows the text.
    Type tells the layers apart: schema (XSD) or business (the rule says which); also xml / namespace.
    """
    for i, e in enumerate(errors):
        line_col, text_col = st.columns([1, 7], vertical_alignment="top")
        with line_col:
            if goto and e.line:
                st.button(
                    f"Line {e.line}", key=f"{key}_goto_{i}", on_click=goto, args=(e.line,),
                    help="Show this line in the editor", width="stretch",
                )
            else:
                st.markdown(f"**Line {e.line}**" if e.line else "**Line ?**")
        with text_col:
            st.html(_error_html(e))


def render_result(result, key: str = "result", goto=None) -> None:
    if result.schema_version:
        st.info(f"Schema used: {result.schema_version}")
    if result.valid:
        st.success("VALID")
        if result.business_rules_apply:
            st.caption("Passed the schema and the business rules.")
        else:
            st.caption("Passed the schema. There are no business rules for this message type yet.")
    else:
        st.error(f"INVALID — {len(result.errors)} error(s)")
        error_list(result.errors, key, goto)
        if result.schema_version and result.business_rules_apply and not result.business_checked:
            st.info("Business rules were not checked: fix the schema errors first.")


def _open_in_test(data: bytes, line: int) -> None:
    """Upload mode, 'Line N': open the file in the Test editor, validated, and jump to that line."""
    text = data.decode("utf-8-sig", errors="replace")
    st.session_state["test_content"] = text
    st.session_state["test_checked"] = (text, validate_bytes(pasted_to_bytes(text)))
    st.session_state["goto_line"] = line
    st.session_state["mode"] = TEST


def show_result(data: bytes) -> bool:
    result = validate_bytes(data)
    render_result(result, "upload", lambda line: _open_in_test(data, line))
    return result.valid


def color_button(key: str, valid: bool) -> None:
    """Paint the keyed button green (valid) or red (invalid)."""
    color = "#1a7f37" if valid else "#cf222e"
    st.markdown(
        f"<style>.st-key-{key} button {{ background-color: {color}; border-color: {color}; color: white; }}"
        f".st-key-{key} button:hover {{ background-color: {color}; border-color: {color}; color: white; opacity: .85; }}</style>",
        unsafe_allow_html=True,
    )


# ---- Template form (shared by Generate and Batch Generate) ----------------------------------

_FIELD_NAMES = [
    "msg_id",
    "debtor_name",
    "debtor_iban",
    "creditor_name",
    "creditor_iban",
    "amount",
    "currency",
    "execution_date",
    "end_to_end_id",
]


def _key(prefix: str, name: str) -> str:
    return f"{prefix}_{name}"


def _load_template_into_form(prefix: str, template) -> None:
    for name in _FIELD_NAMES:
        value = getattr(template.fields, name)
        if name == "execution_date" and _PLAIN_DATE.fullmatch(value):
            value = date.fromisoformat(value)
        st.session_state[_key(prefix, name)] = value
    st.session_state[_key(prefix, "result")] = None


def _open_template(prefix: str, label: str):
    """Template upload + checks. Returns (bytes, Template, is_new) or None if there is nothing to edit yet."""
    upload = st.file_uploader(label, type=["xml"], key=_key(prefix, "template"))
    if upload is None:
        st.info("Upload a valid pain.001 message to use as a template.")
        return None

    data = upload.getvalue()
    try:
        template = read_template(data)
    except TemplateError as exc:
        st.error(str(exc))
        if exc.errors:
            error_list(exc.errors, f"{prefix}_template")
        return None

    digest = hashlib.sha256(data).hexdigest()
    is_new = st.session_state.get(_key(prefix, "digest")) != digest
    if is_new:
        st.session_state[_key(prefix, "digest")] = digest
        _load_template_into_form(prefix, template)

    st.info(f"Template schema: {template.schema_version}")
    if template.transaction_count > 1:
        st.caption(
            f"This template has {template.transaction_count} transactions; only the first is edited "
            "(multi-transaction editing comes later)."
        )
    return data, template, is_new


def _date_input(label: str, key: str) -> None:
    if isinstance(st.session_state[key], date):
        st.date_input(label, key=key)
    else:  # e.g. a date with a timezone suffix: keep it as text, untouched
        st.text_input(label, key=key)


def _form_fields(prefix: str, *, ids: bool) -> None:
    """The editable fields. Call inside st.form. `ids` adds MsgId and EndToEndId."""
    if ids:
        st.text_input("Message ID (MsgId)", key=_key(prefix, "msg_id"))
        st.text_input("Payment reference (EndToEndId)", key=_key(prefix, "end_to_end_id"))
    left, right = st.columns(2)
    with left:
        st.text_input("Debtor name", key=_key(prefix, "debtor_name"))
        st.text_input("Debtor IBAN", key=_key(prefix, "debtor_iban"))
    with right:
        st.text_input("Creditor name", key=_key(prefix, "creditor_name"))
        st.text_input("Creditor IBAN", key=_key(prefix, "creditor_iban"))
    amount, currency, when = st.columns(3)
    with amount:
        st.text_input("Amount" if ids else "Base amount (varied per file)", key=_key(prefix, "amount"))
    with currency:
        st.text_input("Currency", key=_key(prefix, "currency"))
    with when:
        _date_input("Requested execution date", _key(prefix, "execution_date"))


def _read_form(prefix: str) -> MessageFields:
    values = {}
    for name in _FIELD_NAMES:
        value = st.session_state[_key(prefix, name)]
        values[name] = value.isoformat() if isinstance(value, date) else value.strip()
    return MessageFields(**values)


# ---- Generate mode -------------------------------------------------------------------------


def _on_generate(data: bytes) -> None:
    fields = _read_form("gen")
    st.session_state["gen_result"] = (generate(data, fields), fields)
    # Unedited generated IDs are used up; roll new ones so the next message cannot duplicate them.
    if fields.msg_id == st.session_state["gen_auto_msg_id"]:
        st.session_state["gen_msg_id"] = st.session_state["gen_auto_msg_id"] = new_msg_id()
    if fields.end_to_end_id == st.session_state["gen_auto_e2e_id"]:
        st.session_state["gen_end_to_end_id"] = st.session_state["gen_auto_e2e_id"] = new_end_to_end_id()


def generate_mode() -> None:
    opened = _open_template("gen", "Upload a valid pain.001 XML as a template")
    if opened is None:
        return
    data, _template, is_new = opened
    if is_new:  # IDs are generated, not taken from the template
        st.session_state["gen_msg_id"] = st.session_state["gen_auto_msg_id"] = new_msg_id()
        st.session_state["gen_end_to_end_id"] = st.session_state["gen_auto_e2e_id"] = new_end_to_end_id()

    with st.form("gen_form"):
        _form_fields("gen", ids=True)
        st.form_submit_button("Generate", on_click=_on_generate, args=(data,))

    outcome = st.session_state.get("gen_result")
    if outcome is None:
        return
    result, used = outcome
    if result.ok:
        st.success(f"Generated a valid {result.schema_version} message.")
        file_name = re.sub(r"[^A-Za-z0-9._-]", "_", used.msg_id) + ".xml"
        st.download_button(
            "Download XML", result.xml, file_name=file_name, mime="application/xml", on_click="ignore"
        )
        with st.expander("Preview"):
            st.code(result.xml.decode("utf-8", errors="replace"), language="xml")
    else:
        st.error(f"The edited message is not valid — {len(result.errors)} error(s). No file was produced.")
        error_list(result.errors, "gen_result")


# ---- Batch Generate mode ---------------------------------------------------------------------


def _on_generate_batch(data: bytes) -> None:
    try:
        batch = generate_batch(
            data,
            _read_form("bat"),
            count=int(st.session_state["bat_count"]),
            valid_percent=st.session_state["bat_valid_percent"],
        )
        st.session_state["bat_result"] = batch
    except BatchError as exc:
        st.session_state["bat_result"] = exc


def batch_generate_mode() -> None:
    opened = _open_template("bat", "Upload a valid pain.001 XML as the template for the batch")
    if opened is None:
        return
    data, _template, _is_new = opened

    with st.form("bat_form"):
        st.caption("MsgId and EndToEndId are generated for every file. The amount varies around the base amount.")
        _form_fields("bat", ids=False)
        count, ratio = st.columns(2)
        with count:
            st.number_input("Number of files", min_value=1, max_value=MAX_FILES, value=10, step=1, key="bat_count")
        with ratio:
            st.slider("Valid files (%)", min_value=0, max_value=100, value=80, step=5, key="bat_valid_percent")
        st.form_submit_button("Generate batch", on_click=_on_generate_batch, args=(data,))

    outcome = st.session_state.get("bat_result")
    if outcome is None:
        return
    if isinstance(outcome, BatchError):
        st.error(f"{outcome} No zip was produced.")
        error_list(outcome.errors, "bat_result")
        return
    valid = sum(f.valid for f in outcome.files)
    st.success(
        f"Generated {len(outcome.files)} {outcome.schema_version} files: "
        f"{valid} valid, {len(outcome.files) - valid} invalid."
    )
    st.download_button(
        "Download ZIP", outcome.zip_bytes, file_name="pain001_test_suite.zip", mime="application/zip", on_click="ignore"
    )
    st.dataframe(outcome.manifest, width="stretch", hide_index=True)


# ---- Simulate mode ---------------------------------------------------------------------------

_STATUS_BY_LABEL = {label: code for code, label in STATUSES.items()}
_REASON_BY_LABEL = {f"{code} · {name}": code for code, name in REASON_CODES.items()}


def _on_simulate(data: bytes, count: int, digest: str) -> None:
    state = st.session_state
    decisions = []
    for i in range(count):
        status = _STATUS_BY_LABEL[state[f"sim_{digest}_status_{i}"]]
        reason = _REASON_BY_LABEL[state[f"sim_{digest}_reason_{i}"]] if status == REJECTED else None
        decisions.append(Decision(status, reason))
    state["sim_result"] = (digest, simulate(data, decisions))


def simulate_mode() -> None:
    """Valid pain.001 in, the bank's pain.002 reply out: one status per transaction, all Accepted by default."""
    upload = st.file_uploader("Upload a valid pain.001 XML to answer", type=["xml"], key="sim_upload")
    if upload is None:
        st.info("Upload a valid pain.001 message to simulate the bank's reply.")
        return
    data = upload.getvalue()
    try:
        original = read_original(data)
    except SimulationError as exc:
        st.error(str(exc))
        if exc.errors:
            error_list(exc.errors, "sim_input")
        return

    digest = hashlib.sha256(data).hexdigest()[:12]
    st.info(
        f"Original message {original.msg_id} ({original.schema_version}, created {original.created}). "
        f"The reply will be a {original.response_version}."
    )
    st.caption(f"{len(original.transactions)} transaction(s). Everything is accepted unless you change it.")

    esc = html.escape
    for i, tx in enumerate(original.transactions):
        who, status_col, reason_col = st.columns([4, 3, 5], vertical_alignment="center")
        with who:
            ids = " · ".join(filter(None, [f"PmtInfId {tx.pmt_inf_id}", f"InstrId {tx.instr_id}" if tx.instr_id else None]))
            st.html(
                f'<div style="line-height:1.35;overflow-wrap:anywhere"><b>{esc(tx.end_to_end_id)}</b><br>'
                f'<span style="font-size:12px;opacity:.75">{esc(ids)} · {esc(tx.amount)} {esc(tx.currency)} → {esc(tx.creditor_name)}</span></div>'
            )
        with status_col:
            status = st.selectbox(
                f"Status of {tx.end_to_end_id}", list(STATUSES.values()), key=f"sim_{digest}_status_{i}", label_visibility="collapsed"
            )
        with reason_col:
            st.selectbox(
                f"Reason for {tx.end_to_end_id}",
                list(_REASON_BY_LABEL),
                key=f"sim_{digest}_reason_{i}",
                disabled=_STATUS_BY_LABEL[status] != REJECTED,
                label_visibility="collapsed",
            )
    st.button(
        "Generate pain.002", key="sim_generate", on_click=_on_simulate, args=(data, len(original.transactions), digest)
    )

    outcome = st.session_state.get("sim_result")
    if outcome is None or outcome[0] != digest:
        return
    result = outcome[1]
    if result.ok:
        st.success(f"Generated a valid {result.schema_version} reply ({result.msg_id}).")
        st.download_button(
            "Download pain.002", result.xml, file_name=file_name(result), mime="application/xml", on_click="ignore"
        )
        with st.expander("Preview"):
            st.code(result.xml.decode("utf-8", errors="replace"), language="xml")
    else:
        st.error(f"The reply is not valid — {len(result.errors)} error(s). No file was produced.")
        error_list(result.errors, "sim_result")


# ---- Test mode ------------------------------------------------------------------------------


def test_mode() -> None:
    """An edit-and-revalidate workspace: edit the XML, click Validate, fix, click again."""
    state = st.session_state
    content = state.get("test_content", "")  # survives reruns and switching to another mode
    checked = state.get("test_checked")  # (text, result) of the last Validate click
    marked = editor.annotations_for(checked[1].errors) if checked and checked[0] == content else []

    text = editor.xml_editor(content, "test_editor", marked)
    state["test_content"] = text
    if marked and text != content:
        st.rerun()  # the text was edited: markers for the old text must not stay on screen
    if goto_line := state.pop("goto_line", None):  # an error's "Line N" button was clicked: one run only
        editor.scroll_to_line(goto_line)

    if st.button("Validate", key="validate_btn"):
        if not text.strip():
            state.pop("test_checked", None)
            st.info("Type or paste an XML message in the editor, then click Validate.")
            return
        state["test_checked"] = (text, validate_bytes(pasted_to_bytes(text)))
        st.rerun()  # redraw the editor with the error markers

    checked = state.get("test_checked")
    if checked is None:
        if not text.strip():
            st.info("Type or paste an XML message in the editor, then click Validate.")
    elif checked[0] == text:
        render_result(checked[1], "test", lambda line: state.__setitem__("goto_line", line))
        color_button("validate_btn", checked[1].valid)
    else:
        st.caption("The XML has changed since it was last validated. Click Validate to check it again.")


# ---- Page -----------------------------------------------------------------------------------

st.set_page_config(page_title="ISO 20022 Validator", page_icon="✅")
st.title("ISO 20022 Validator")
st.caption("v0.5 · pain.001 / pain.002 · validation, generation, test suites and simulated bank replies")

mode = st.radio("Input", [UPLOAD, TEST, GENERATE, BATCH, SIMULATE], horizontal=True, label_visibility="collapsed", key="mode")

if mode == UPLOAD:
    upload = st.file_uploader("Upload a pain.001 or pain.002 XML file", type=["xml"])
    if upload is not None:
        show_result(upload.getvalue())
elif mode == TEST:
    test_mode()
elif mode == GENERATE:
    generate_mode()
elif mode == BATCH:
    batch_generate_mode()
else:
    simulate_mode()
