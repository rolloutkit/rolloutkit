"""Measurement reports identify the harness revision that produced them."""

from __future__ import annotations

import sys
import types

import pytest

from rolloutkit import provenance


def _stamp(monkeypatch: pytest.MonkeyPatch, commit: str) -> None:
    """Present the module a build writes, without building anything."""
    module = types.ModuleType("rolloutkit._build_info")
    module.COMMIT = commit  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rolloutkit._build_info", module)


def test_explicit_commit_wins(monkeypatch) -> None:
    monkeypatch.setenv("ROLLOUTKIT_COMMIT", "ABCDEF1234567")

    assert provenance.rolloutkit_commit() == "abcdef1234567"


def test_invalid_explicit_commit_is_not_reported(monkeypatch) -> None:
    monkeypatch.setenv("ROLLOUTKIT_COMMIT", "main; not-a-commit")

    assert provenance.rolloutkit_commit() == "unknown"


def test_checkout_commit_is_resolved_without_an_override(monkeypatch) -> None:
    monkeypatch.delenv("ROLLOUTKIT_COMMIT", raising=False)

    commit = provenance.rolloutkit_commit()

    assert len(commit) == 40
    assert set(commit) <= set("0123456789abcdef")


def test_packaged_install_without_git_reports_unknown(monkeypatch) -> None:
    monkeypatch.delenv("ROLLOUTKIT_COMMIT", raising=False)
    monkeypatch.setattr(provenance, "_checkout_root", lambda _start: None)

    assert provenance.rolloutkit_commit() == "unknown"


def test_a_stamped_build_answers_where_a_checkout_would_not(monkeypatch) -> None:
    """What an installed copy has instead of Git.

    This test runs inside the checkout, so the Git source would answer too —
    which is exactly the condition that hid the defect. On a user's machine
    there is no checkout, and until the build started writing this module the
    only answer available there was `"unknown"`.
    """
    monkeypatch.delenv("ROLLOUTKIT_COMMIT", raising=False)
    _stamp(monkeypatch, "A" * 40)

    assert provenance.rolloutkit_commit() == "a" * 40


def test_an_explicit_commit_outranks_the_stamp(monkeypatch) -> None:
    """The Docker matrix names the revision it is exercising, and that wins.

    CI installs one build and runs it against the checkout it is testing, so
    the stamp and the truth can legitimately differ there.
    """
    monkeypatch.setenv("ROLLOUTKIT_COMMIT", "b" * 40)
    _stamp(monkeypatch, "a" * 40)

    assert provenance.rolloutkit_commit() == "b" * 40


def test_a_malformed_stamp_does_not_fall_through_to_a_checkout(monkeypatch) -> None:
    """A built copy that cannot read its own stamp has nothing else to consult.

    Falling through would send an installed copy looking for a checkout above
    `site-packages`, and any checkout found there belongs to somebody else.
    """
    monkeypatch.delenv("ROLLOUTKIT_COMMIT", raising=False)
    _stamp(monkeypatch, "not-a-commit")

    assert provenance.rolloutkit_commit() == "unknown"


def test_the_checkout_that_tracks_this_package_is_the_answer(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    source = tmp_path / "src" / "rolloutkit"
    source.mkdir(parents=True)

    assert provenance._checkout_root(source / "provenance.py") == tmp_path


def test_a_checkout_this_package_merely_sits_inside_is_refused(tmp_path) -> None:
    """A venv created inside an unrelated project must not borrow its HEAD.

    This is the layout: somebody runs `python -m venv .venv` in their own
    repository and installs rolloutkit into it. Walking up from `site-packages`
    reaches that repository's `.git`, and reporting its HEAD as
    `rolloutkit_commit` is worse than reporting nothing — a plausible wrong SHA
    is indistinguishable from a right one until somebody tries to check it out.
    """
    (tmp_path / ".git").mkdir()
    installed = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "rolloutkit"
    installed.mkdir(parents=True)

    assert provenance._checkout_root(installed / "provenance.py") is None
