"""Import checks: a broken import must fail here, not only when the app is deployed.

Why these exist: a deploy once failed with `ImportError: cannot import name 'REASON_CODES' from
'iso20022_validator.core'` although the repo was correct and every test passed. The tests ran against the
working tree; the host ran the new app.py against an older *installed copy* of the package. So there are two
kinds of check: the names the app imports must exist and be exported, and the app must still start when a stale
copy of the package is installed.
"""

import ast
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = SRC / "iso20022_validator"
APP = PACKAGE / "app.py"
MODULES = sorted(PACKAGE.rglob("*.py"))


def imports_from_the_package(path: Path):
    """(module, name) for every `from iso20022_validator[.x] import name` in a source file."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "iso20022_validator":
            for alias in node.names:
                yield node.module, alias.name


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_every_name_imported_from_the_package_exists(path):
    missing = []
    for module, name in imports_from_the_package(path):
        mod = importlib.import_module(module)
        if not hasattr(mod, name) and not _is_submodule(module, name):
            missing.append(f"{module}.{name}")
    assert not missing, f"{path.name} imports names that do not exist: {missing}"


def _is_submodule(module: str, name: str) -> bool:
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except ImportError:
        return False


def test_every_name_the_app_takes_from_core_is_exported_in_all():
    core = importlib.import_module("iso20022_validator.core")
    wanted = {name for module, name in imports_from_the_package(APP) if module == "iso20022_validator.core"}
    assert wanted, "app.py is expected to import from iso20022_validator.core"
    assert not wanted - set(core.__all__), f"imported by app.py but not in core.__all__: {sorted(wanted - set(core.__all__))}"


def test_everything_in_core_all_exists():
    core = importlib.import_module("iso20022_validator.core")
    assert [n for n in core.__all__ if not hasattr(core, n)] == []


def run_python(code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, "-W", "ignore", "-c", textwrap.dedent(code)], capture_output=True, text=True, env=env, timeout=120)


def test_the_app_module_imports_in_a_clean_interpreter():
    """What `streamlit run` does first, without pytest's sys.modules or working directory helping."""
    result = run_python(f"import runpy; runpy.run_path({str(APP)!r}, run_name='app')")
    assert result.returncode == 0, result.stderr[-1500:]


def test_the_app_still_starts_when_an_older_copy_of_the_package_is_installed(tmp_path):
    """The deploy failure: a stale `iso20022_validator` earlier on the path than this repo's. The app must use the repo's."""
    stale = tmp_path / "stale" / "iso20022_validator"
    (stale / "core").mkdir(parents=True)
    (stale / "__init__.py").write_text("")
    (stale / "editor.py").write_text("")
    (stale / "core" / "__init__.py").write_text("validate_bytes = None  # an old release: none of the newer names\n")

    result = run_python(
        f"""
        import runpy, sys
        sys.path.insert(0, {str(tmp_path / 'stale')!r})   # the stale copy is what is installed ...
        sys.path.insert(0, {str(PACKAGE)!r})              # ... and streamlit puts the script's own folder first
        runpy.run_path({str(APP)!r}, run_name='app')
        import iso20022_validator
        print(iso20022_validator.__file__)
        """
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() == (PACKAGE / "__init__.py").resolve()  # the repo's copy
