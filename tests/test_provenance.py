"""Measurement reports identify the harness revision that produced them."""

from __future__ import annotations

from preflightkit import provenance


def test_explicit_commit_wins(monkeypatch) -> None:
    monkeypatch.setenv("PREFLIGHTKIT_COMMIT", "ABCDEF1234567")

    assert provenance.preflightkit_commit() == "abcdef1234567"


def test_invalid_explicit_commit_is_not_reported(monkeypatch) -> None:
    monkeypatch.setenv("PREFLIGHTKIT_COMMIT", "main; not-a-commit")

    assert provenance.preflightkit_commit() == "unknown"


def test_checkout_commit_is_resolved_without_an_override(monkeypatch) -> None:
    monkeypatch.delenv("PREFLIGHTKIT_COMMIT", raising=False)

    commit = provenance.preflightkit_commit()

    assert len(commit) == 40
    assert set(commit) <= set("0123456789abcdef")


def test_packaged_install_without_git_reports_unknown(monkeypatch) -> None:
    monkeypatch.delenv("PREFLIGHTKIT_COMMIT", raising=False)
    monkeypatch.setattr(provenance, "_checkout_root", lambda _start: None)

    assert provenance.preflightkit_commit() == "unknown"
