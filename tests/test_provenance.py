"""Measurement reports identify the harness revision that produced them."""

from __future__ import annotations

from rolloutkit import provenance


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
