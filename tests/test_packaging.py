"""The distribution metadata, checked against the package it describes.

None of this needs Docker or a network. It exists because the two places the
version is written are not connected to each other: `pyproject.toml` is read by
the build backend and `__init__.py` is read at runtime, and a release that
disagrees with itself reports one number in `--version` and another on PyPI.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import preflightkit

ROOT = Path(__file__).resolve().parent.parent


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_the_runtime_version_matches_the_distribution_version() -> None:
    assert preflightkit.__version__ == _project()["version"], (
        "src/preflightkit/__init__.py and pyproject.toml disagree about the "
        "version. `--version` reports the first and PyPI publishes the second."
    )


def test_the_licence_file_the_metadata_names_is_present() -> None:
    project = _project()
    assert project["license"] == "Apache-2.0"
    for pattern in project["license-files"]:
        assert (ROOT / pattern).is_file(), f"license-files names {pattern}, which is absent"


def test_no_license_classifier_alongside_an_spdx_expression() -> None:
    """PyPI rejects a distribution that states its licence twice.

    The SPDX `license` field above puts the metadata at 2.4, where the
    `License ::` classifiers are the superseded spelling. Keeping one of each is
    the mistake this catches, and it is only visible at upload time otherwise.
    """
    offenders = [c for c in _project()["classifiers"] if c.startswith("License ::")]
    assert not offenders, offenders


def test_the_changelog_documents_the_version_being_shipped() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    version = _project()["version"]
    assert f"## [{version}]" in changelog, (
        f"CHANGELOG.md has no section for {version}. A release with nothing "
        "written about it is a release nobody can read."
    )
