"""Navigation: category -> message type (sidebar), the modes of that message type as tabs, the detected-schema
indicator, and the messages/<area>/<number>/ folder layout the catalog and the plugin discovery rely on."""

import shutil
from pathlib import Path

import pytest
import streamlit as st
from helpers import current_tab, detected_schema, errors_shown, select_message_type, select_tab, tab_labels
from lxml import etree
from streamlit.testing.v1 import AppTest

from iso20022_validator import catalog
from iso20022_validator.core import engine
from iso20022_validator.core.simulate import simulate as run_simulation
from iso20022_validator.core.engine import supported_namespaces, version_label

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
MESSAGES = ROOT / "src" / "iso20022_validator" / "messages"
SAMPLES = ROOT / "samples" / "pain.001"
V09 = SAMPLES / "pain.001.001.09"
V03 = SAMPLES / "pain.001.001.03"
ALL_MODES = ["Upload file", "Test", "Generate", "Batch Generate", "Simulate"]


@pytest.fixture
def plain_editor(monkeypatch):
    def fake(value, key, annotations=None):
        return st.text_area("XML editor", value=value, key=key, height=300)

    monkeypatch.setattr("iso20022_validator.editor.xml_editor", fake)


def start() -> AppTest:
    at = AppTest.from_file(APP).run()
    assert not at.exception
    return at


def upload(at: AppTest, path: Path) -> AppTest:
    at.file_uploader[0].upload(path.name, path.read_bytes(), "text/xml").run()
    assert not at.exception
    return at


# ---- the catalog ---------------------------------------------------------------------------------


def test_the_catalog_has_payments_initiation_with_pain001_and_pain002():
    (category,) = catalog.available_categories()
    assert (category.area, category.name) == ("pain", "Payments Initiation")
    assert [(t.family, t.title) for t in category.types] == [
        ("pain.001", "Customer Credit Transfer Initiation"),
        ("pain.002", "Customer Payment Status Report"),
    ]


def test_modes_depend_on_the_message_type():
    by_family = {t.family: t for c in catalog.CATALOG for t in c.types}
    assert list(by_family["pain.001"].modes) == ALL_MODES
    assert list(by_family["pain.002"].modes) == ["Upload file", "Test"]  # nothing to generate or simulate from a reply


def test_future_categories_exist_but_are_not_available_yet():
    assert [(c.area, c.name) for c in catalog.coming_soon()] == [
        ("pacs", "Clearing & Settlement"),
        ("camt", "Cash Management"),
        ("admi", "Administration"),
    ]
    assert all(not c.types and not c.available for c in catalog.coming_soon())


def test_find_and_family_of():
    assert catalog.family_of("pain.001.001.09") == "pain.001"
    category, message_type = catalog.find("pain.002.001.10")
    assert (category.area, message_type.family) == ("pain", "pain.002")
    assert catalog.find("pacs.008.001.08") is None


def test_a_message_type_lists_its_installed_schemas():
    by_family = {t.family: t for c in catalog.CATALOG for t in c.types}
    assert by_family["pain.001"].schemas == ["pain.001.001.03", "pain.001.001.09"]
    assert by_family["pain.002"].schemas == ["pain.002.001.03", "pain.002.001.10"]


def test_a_new_message_type_needs_only_a_catalog_entry(monkeypatch):
    """Adding a category's first message type makes it appear; no other code is involved."""
    camt = catalog.Category("camt", "Cash Management", (catalog.MessageType("camt.053", "Bank to Customer Statement", (catalog.UPLOAD,)),))
    monkeypatch.setattr(catalog, "CATALOG", tuple(camt if c.area == "camt" else c for c in catalog.CATALOG))
    assert [c.area for c in catalog.available_categories()] == ["pain", "camt"]
    assert "Cash Management" not in [c.name for c in catalog.coming_soon()]


# ---- the folder layout and the plugin discovery -------------------------------------------------------


def schema_files() -> list[Path]:
    return sorted(MESSAGES.glob("*/*/schema.xsd"))


def test_schemas_live_in_messages_area_number_folders():
    assert len(schema_files()) == 4
    assert list(MESSAGES.glob("*/schema.xsd")) == []  # nothing left in the old flat layout
    assert not [p for p in MESSAGES.iterdir() if p.is_dir() and p.name.startswith("pain_")]


@pytest.mark.parametrize("xsd", schema_files(), ids=lambda p: f"{p.parent.parent.name}/{p.parent.name}")
def test_each_folder_name_matches_its_schema_identifier(xsd):
    """messages/pain/001_001_09/ <-> targetNamespace ...:pain.001.001.09"""
    namespace = etree.parse(str(xsd)).getroot().get("targetNamespace")
    identifier = version_label(namespace)  # pain.001.001.09
    area, number = xsd.parent.parent.name, xsd.parent.name
    assert identifier == f"{area}." + number.replace("_", ".")


def test_every_installed_schema_belongs_to_a_catalog_type_in_the_right_category():
    for namespace in supported_namespaces():
        found = catalog.find(version_label(namespace))
        assert found, f"{version_label(namespace)} is not in the catalog"
        category, _ = found
        assert version_label(namespace).startswith(category.area + ".")


def test_every_catalog_type_has_at_least_one_schema():
    for category in catalog.available_categories():
        for message_type in category.types:
            assert message_type.schemas, f"{message_type.family} has no schema installed"


def test_discovery_reads_area_number_folders_and_ignores_the_old_flat_layout(tmp_path):
    new = tmp_path / "camt" / "001_001_09"  # an area nothing knows about: discovery is not hard-coded
    new.mkdir(parents=True)
    shutil.copy(MESSAGES / "pain" / "001_001_09" / "schema.xsd", new / "schema.xsd")
    old = tmp_path / "pain_001_001_03"  # the layout from before the regrouping
    old.mkdir()
    shutil.copy(MESSAGES / "pain" / "001_001_03" / "schema.xsd", old / "schema.xsd")

    real = engine.MESSAGES_DIR
    engine.MESSAGES_DIR = tmp_path
    engine.supported_namespaces.cache_clear()
    try:
        found = engine.supported_namespaces()
        assert [p.parent.parent.name for p in found.values()] == ["camt"]
        assert len(found) == 1  # the flat folder was not picked up
    finally:
        engine.MESSAGES_DIR = real
        engine.supported_namespaces.cache_clear()
    assert len(engine.supported_namespaces()) == 4


def test_validation_is_unchanged_by_the_regrouping():
    from iso20022_validator import validate_file

    for version in ("pain.001.001.03", "pain.001.001.09"):
        assert validate_file(SAMPLES / version / "valid_single_payment.xml").valid
        assert not validate_file(SAMPLES / version / "invalid_missing_msgid.xml").valid


# ---- the sidebar -------------------------------------------------------------------------------------


def test_the_sidebar_has_category_and_message_type_dropdowns():
    at = start()
    category, message_type = at.sidebar.selectbox
    assert (category.label, message_type.label) == ("Category", "Message type")
    assert category.options == ["Payments Initiation"]  # only what exists; the rest is "coming soon"
    assert message_type.options == ["pain.001 · Customer Credit Transfer Initiation", "pain.002 · Customer Payment Status Report"]
    assert (category.value, message_type.value) == ("pain", "pain.001")  # pain.001 is the default


def test_categories_without_message_types_are_named_as_coming_soon_not_offered():
    at = start()
    assert "Coming soon: Clearing & Settlement, Cash Management, Administration" in [c.value for c in at.sidebar.caption]
    assert at.sidebar.selectbox(key="nav_category").options == ["Payments Initiation"]


def test_the_sidebar_shows_the_full_title_of_the_selected_message_type():
    at = start()
    assert "Customer Credit Transfer Initiation" in [c.value for c in at.sidebar.caption]
    assert "Customer Payment Status Report" in [c.value for c in select_message_type(at, "pain.002").sidebar.caption]


def test_the_sidebar_lists_the_schemas_of_the_selected_message_type():
    at = start()
    assert "Schemas: pain.001.001.03, pain.001.001.09" in [c.value for c in at.sidebar.caption]
    select_message_type(at, "pain.002")
    assert "Schemas: pain.002.001.03, pain.002.001.10" in [c.value for c in at.sidebar.caption]


# ---- tabs follow the message type ------------------------------------------------------------------------


def test_pain001_shows_all_five_tabs():
    assert tab_labels(start()) == ALL_MODES


def test_pain002_shows_only_the_tabs_that_apply_to_it():
    at = select_message_type(start(), "pain.002")
    assert not at.exception
    assert tab_labels(at) == ["Upload file", "Test"]
    for gone in ("Generate", "Batch Generate", "Simulate"):
        assert gone not in tab_labels(at)
    assert tab_labels(select_message_type(at, "pain.001")) == ALL_MODES  # and back


def test_only_the_open_tab_is_built(plain_editor):
    at = start()
    assert len(at.file_uploader) == 1 and len(at.text_area) == 0  # Upload is open: no editor yet
    select_tab(at, "Test")
    assert len(at.file_uploader) == 0 and len(at.text_area) == 1
    select_tab(at, "Simulate")
    assert len(at.text_area) == 0 and at.file_uploader[0].label == "Upload a valid pain.001 XML to answer"


def test_each_message_type_remembers_its_own_tab():
    at = start()
    select_tab(at, "Generate")
    select_message_type(at, "pain.002")
    assert current_tab(at, "pain.002") == "Upload file"  # pain.002 has its own, starting at the first tab
    select_tab(at, "Test", "pain.002")
    select_message_type(at, "pain.001")
    assert current_tab(at, "pain.001") == "Generate"  # unchanged by what happened to pain.002
    assert at.file_uploader[0].label == "Upload a valid pain.001 XML as a template"


@pytest.mark.parametrize("mode", ALL_MODES)
def test_every_pain001_tab_renders_without_an_error(mode, plain_editor):
    at = select_tab(start(), mode)
    assert not at.exception


@pytest.mark.parametrize("mode", ["Upload file", "Test"])
def test_every_pain002_tab_renders_without_an_error(mode, plain_editor):
    at = select_tab(select_message_type(start(), "pain.002"), mode, "pain.002")
    assert not at.exception


def test_the_upload_label_names_the_selected_message_type():
    at = start()
    assert at.file_uploader[0].label == "Upload a pain.001 XML file"
    assert select_message_type(at, "pain.002").file_uploader[0].label == "Upload a pain.002 XML file"


def test_pain002_validation_works_in_the_pain002_tabs(plain_editor):
    reply = run_simulation((V09 / "valid_three_payments.xml").read_bytes()).xml
    at = select_message_type(start(), "pain.002")
    at.file_uploader[0].upload("reply.xml", reply, "text/xml").run()
    assert [s.value for s in at.success] == ["VALID"]
    assert not any("Note:" in c.value for c in at.caption)  # the file is what is selected

    select_tab(at, "Test", "pain.002")
    at.text_area(key="test_editor").input(reply.decode("utf-8")).run()
    at.button(key="validate_btn").click().run()
    assert [s.value for s in at.success] == ["VALID"] and detected_schema(at) == "pain.002.001.10"


def test_a_file_of_another_message_type_is_still_validated_with_a_note():
    reply = run_simulation((V09 / "valid_single_payment.xml").read_bytes()).xml
    at = start()  # pain.001 is selected
    at.file_uploader[0].upload("reply.xml", reply, "text/xml").run()
    assert [s.value for s in at.success] == ["VALID"]
    assert "Note: this message is pain.002.001.10, but pain.001 is selected in the sidebar." in [c.value for c in at.caption]
    assert detected_schema(at) == "pain.002.001.10"  # the indicator says what it really is


# ---- the "Detected schema" indicator -------------------------------------------------------------------------


def test_nothing_is_detected_before_a_message_is_given():
    at = start()
    assert detected_schema(at) is None
    assert "Detected schema" in [c.value for c in at.sidebar.caption]


@pytest.mark.parametrize("version", ["pain.001.001.03", "pain.001.001.09"])
def test_an_uploaded_message_shows_its_exact_schema(version):
    at = upload(start(), SAMPLES / version / "valid_single_payment.xml")
    assert detected_schema(at) == version


@pytest.mark.parametrize("version", ["pain.001.001.03", "pain.001.001.09"])
def test_an_invalid_message_still_shows_its_schema(version):
    assert detected_schema(upload(start(), SAMPLES / version / "invalid_missing_msgid.xml")) == version


def test_an_unknown_namespace_is_reported_as_not_recognised():
    assert detected_schema(upload(start(), V03 / "invalid_unsupported_namespace.xml")) == "not recognised"


def test_a_file_that_is_not_xml_detects_nothing():
    assert detected_schema(upload(start(), V03 / "invalid_not_wellformed.xml")) is None


@pytest.mark.parametrize("version", ["pain.001.001.03", "pain.001.001.09"])
@pytest.mark.parametrize("mode", ["Generate", "Batch Generate", "Simulate"])
def test_the_template_modes_show_the_schema_of_the_template(version, mode):
    at = select_tab(start(), mode)
    path = SAMPLES / version / "valid_single_payment.xml"
    at.file_uploader[0].upload(path.name, path.read_bytes(), "text/xml").run()
    assert detected_schema(at) == version


def test_simulate_shows_the_original_schema_and_the_schema_it_generated():
    at = select_tab(start(), "Simulate")
    path = V09 / "valid_three_payments.xml"
    at.file_uploader[0].upload(path.name, path.read_bytes(), "text/xml").run()
    assert detected_schema(at) == "pain.001.001.09"
    assert not any("Generated:" in c.value for c in at.sidebar.caption)
    at.button(key="sim_generate").click().run()
    assert detected_schema(at) == "pain.001.001.09"
    assert "Generated: `pain.002.001.10`" in [c.value for c in at.sidebar.caption]


def test_the_test_tab_shows_the_schema_of_the_validated_text(plain_editor):
    at = select_tab(start(), "Test")
    text = (V03 / "invalid_iban_checksum.xml").read_text(encoding="utf-8")
    at.text_area(key="test_editor").input(text).run()
    assert detected_schema(at) is None  # nothing is validated, so nothing is detected, until Validate
    at.button(key="validate_btn").click().run()
    assert detected_schema(at) == "pain.001.001.03"
    assert "Detected schema" in [c.value for c in at.sidebar.caption]

    at.text_area(key="test_editor").input(text + "\n").run()  # edited: the old result is hidden ...
    assert detected_schema(at) == "pain.001.001.03"
    assert "Detected schema (last validation)" in [c.value for c in at.sidebar.caption]  # ... and the label says so


def test_the_indicator_follows_a_new_upload():
    at = upload(start(), V03 / "valid_single_payment.xml")
    assert detected_schema(at) == "pain.001.001.03"
    upload(at, V09 / "valid_single_payment.xml")
    assert detected_schema(at) == "pain.001.001.09"


def test_the_error_list_still_works_inside_the_tabs():
    at = upload(start(), V09 / "invalid_ctrlsum_mismatch.xml")
    assert errors_shown(at)[0]["Rule"] == "ctrl_sum"
