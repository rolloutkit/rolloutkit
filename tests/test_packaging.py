"""The distribution, checked against the package it describes.

None of this needs Docker or a network. It exists because the two places the
version is written are not connected to each other: `pyproject.toml` is read by
the build backend and `__init__.py` is read at runtime, and a release that
disagrees with itself reports one number in `--version` and another on PyPI.

The probe payload is here for the same reason. It is assembled out of whatever
the installation happens to contain, and a repo checkout contains more than a
`pip install` does — which is how a required dependency that had quietly become
optional went unnoticed until every installed copy was measuring from the host.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

import preflightkit
from preflightkit.cli.main import INFRASTRUCTURE
from preflightkit.engine.lifecycle import PROBE_FALLBACK
from preflightkit.runtime.docker import ProbePackagingError, _traffic_probe_archive

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


def _payload_without(monkeypatch: pytest.MonkeyPatch, package: str) -> bytes:
    """Build the probe payload as an installation missing `package` would."""
    real = importlib.util.find_spec

    def find_spec(name: str, *args: object, **kwargs: object) -> object:
        return None if name == package else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    return _traffic_probe_archive()


def test_anyio_runs_on_asyncio_without_sniffio() -> None:
    """The premise for skipping sniffio, measured against the anyio installed.

    anyio imports sniffio inside a `try`/`except ImportError` and, without it,
    assumes asyncio — which is the backend the probe runs on. That is why the
    payload may leave it out. If a future anyio makes it load-bearing again this
    fails, which is the signal to move it back to the required list rather than
    to discover it as a fleet of silent host fallbacks.
    """
    script = (
        "import sys\n"
        "sys.modules['sniffio'] = None\n"  # makes `import sniffio` raise
        "import anyio\n"
        "async def main():\n"
        "    await anyio.sleep(0)\n"
        "    return 'ok'\n"
        "print(anyio.run(main))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_probe_payload_builds_without_sniffio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean install has no sniffio, and the sidecar has to start anyway.

    sniffio stopped being an anyio requirement, so `pip install preflightkit`
    resolves without it. Requiring it here meant the payload could not be built,
    the sidecar never started, and every run on every such installation measured
    from the host instead — reported as a fallback the environment had asked
    for, which no environment had.
    """
    payload = _payload_without(monkeypatch, "sniffio")

    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        names = archive.getnames()

    assert not any(name.startswith("pfk_probe/vendor/sniffio") for name in names)
    assert any(name.startswith("pfk_probe/vendor/anyio/") for name in names)
    assert "pfk_probe/sidecar_entry.py" in names


@pytest.mark.parametrize("package", ["anyio", "idna", "typing_extensions"])
def test_a_missing_required_dependency_stops_the_run(
    monkeypatch: pytest.MonkeyPatch, package: str
) -> None:
    with pytest.raises(ProbePackagingError) as raised:
        _payload_without(monkeypatch, package)

    assert package in str(raised.value)
    assert "reinstall" in str(raised.value).lower()


def test_a_broken_install_is_not_answered_with_the_host_fallback() -> None:
    """The two reasons a sidecar does not start are not the same reason.

    A Docker refusal describes this host, and measuring from the host is the
    right answer to it: it costs precision, the report says what it cost, and
    another machine would not have needed it. A payload that cannot be built
    describes the installation — no host would have worked and the next run
    repeats it — so the run stops with exit code 3 instead of publishing a
    permanently degraded measurement under a note about the environment.
    """
    broken = ProbePackagingError("...")

    assert not isinstance(broken, PROBE_FALLBACK)
    assert isinstance(broken, INFRASTRUCTURE)


def _sidecar_check(tmp_path: Path, environment: dict) -> int:
    report = tmp_path / "run.json"
    report.write_text(json.dumps({"environment": environment}))
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "assert_sidecar.py"), str(report)],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


def test_the_sidecar_check_passes_only_on_a_sidecar_run(tmp_path: Path) -> None:
    """The check the wheel job's verdict rests on, checked itself.

    A guard that cannot fail is worse than no guard: it reports the thing it was
    added to catch as absent. Both directions, because only one of them is the
    one that ever runs.
    """
    assert _sidecar_check(tmp_path, {"probe_location": "sidecar"}) == 0
    assert (
        _sidecar_check(
            tmp_path,
            {"probe_location": "host_fallback", "probe_fallback_reason": "..."},
        )
        == 1
    )
    assert _sidecar_check(tmp_path, {}) == 1
