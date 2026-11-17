"""Keep archive failure diagnostics useful without exposing document contents."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from paperless_inference.archive import (
    ArchiveError,
    _convert_to_pdfa,
    _validate_pdfa_2u,
)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_validation_rule_summary_excludes_private_details(monkeypatch, exit_code):
    """Both a failing report and a failing command retain only numeric rule IDs."""
    report = b"""<report><validationReport isCompliant="false">
    <rule status="failed" clause="6.2.11.7.2" testNumber="1"><context>private</context></rule>
    <rule status="failed" clause="private" testNumber="2"/>
    <rule status="passed" clause="1.2" testNumber="3"/>
    </validationReport></report>"""
    monkeypatch.setattr(
        "paperless_inference.archive.subprocess.run",
        Mock(
            return_value=subprocess.CompletedProcess([], exit_code, report, b"secret")
        ),
    )
    with pytest.raises(ArchiveError) as caught:
        _validate_pdfa_2u(Path("private.pdf"))
    error = caught.value
    assert error.stage == "pdfa_validation"
    assert error.exit_code == exit_code
    assert error.validation_rules == ["6.2.11.7.2:1"]
    assert "private" not in str(error)
    assert "secret" not in repr(vars(error))


@pytest.mark.parametrize("stage", ["pdfa_conversion", "pdfa_validation"])
def test_archive_timeout_has_safe_stage(monkeypatch, tmp_path, stage):
    """Timeouts identify the command stage without retaining captured output."""
    monkeypatch.setattr(
        "paperless_inference.archive.subprocess.run",
        Mock(side_effect=subprocess.TimeoutExpired("private", 1, output=b"secret")),
    )
    with pytest.raises(ArchiveError) as caught:
        if stage == "pdfa_conversion":
            _convert_to_pdfa(tmp_path / "source", tmp_path / "output", tmp_path)
        else:
            _validate_pdfa_2u(tmp_path / "source")
    assert caught.value.stage == stage
    assert caught.value.exit_code is None
    assert "secret" not in repr(vars(caught.value))
