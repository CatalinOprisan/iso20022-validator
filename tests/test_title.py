"""The app's displayed name. Only the title changed: the package, CLI command and repo names stay as they were."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "src" / "iso20022_validator" / "app.py")
TITLE = "ISO 20022 Validator & Simulator"


def test_the_header_shows_the_new_name():
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert [t.value for t in at.title] == [TITLE]


def test_the_readme_heading_matches():
    assert (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0] == f"# {TITLE}"


def test_public_names_are_unchanged():
    """The package name, the CLI entry point and the importable module are what other people already use."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "iso20022-validator"' in pyproject
    assert 'iso20022-validate = "iso20022_validator.cli:main"' in pyproject
    assert (ROOT / "src" / "iso20022_validator" / "__init__.py").is_file()
