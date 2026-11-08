"""Language inference remains compatible without resident profiles in its caller."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from documents.language import detect_language

GERMAN = "Diese Rechnung enthält die Kosten für die Lieferung unserer Waren."
ENGLISH = "This invoice contains the total cost of delivering the ordered goods."


def test_detection_isolated_from_caller(monkeypatch, tmp_path) -> None:
    """Detect Unicode text from any working directory without loading profiles."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    previously_loaded = {name for name in sys.modules if name.startswith("langdetect")}
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(detect_language, [GERMAN, ENGLISH])) == ["de", "en"]
    assert {
        name for name in sys.modules if name.startswith("langdetect")
    } == previously_loaded


def test_empty_content_needs_no_process() -> None:
    """Keep the metadata API's English fallback for empty text."""
    with mock.patch("documents.language.subprocess.run") as run:
        assert detect_language("") == "en"
    run.assert_not_called()


def test_undetectable_content_reports_failure() -> None:
    """Let the metadata caller apply its existing fallback for no language."""
    with pytest.raises(subprocess.CalledProcessError):
        detect_language("1234567890")


@pytest.mark.parametrize("output", ["", "not a language", "de\nen", '{"lang": "de"}'])
def test_reject_invalid_helper_output(output: str) -> None:
    """Reject malformed helper responses before exposing them through the API."""
    with (
        mock.patch(
            "documents.language.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=output),
        ),
        pytest.raises(ValueError, match="Invalid language"),
    ):
        detect_language(GERMAN)


@pytest.mark.parametrize(
    "content", ["", "1234567890", "Hi", "x " * 100, "!@# 42\n" * 20]
)
def test_document_unknown_content(content: str) -> None:
    """Do not persist a guessed language for empty, short or repetitive OCR."""
    from documents.language import detect_document_language

    assert detect_document_language(content) == "und"


@pytest.mark.parametrize(
    "content,expected",
    [
        (GERMAN, "de"),
        (ENGLISH, "en"),
        (ENGLISH * 8 + GERMAN, "en"),
        ("123 !! " + GERMAN + " 999 @@", "de"),
    ],
)
def test_document_detection_is_deterministic(content: str, expected: str) -> None:
    """Pick one repeatable primary language from substantive and mixed OCR text."""
    from documents.language import detect_document_language

    assert [detect_document_language(content) for _ in range(3)] == [expected] * 3


@pytest.mark.parametrize(
    "detected,expected",
    [
        ("zh-cn", "cmn"),
        ("zh-tw", "cmn"),
        ("ar", "arb"),
        ("fa", "pes"),
        ("unsupported", "und"),
    ],
)
def test_document_detection_normalizes_codes(detected: str, expected: str) -> None:
    """Publish canonical codes and treat unsupported detector output as unknown."""
    from documents.language import detect_document_language

    with mock.patch(
        "documents.language.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=detected),
    ):
        assert detect_document_language(ENGLISH) == expected


def test_document_detection_failure_is_unknown() -> None:
    """A failed detector must not prevent document publication."""
    from documents.language import detect_document_language

    with mock.patch(
        "documents.language.subprocess.run",
        side_effect=subprocess.TimeoutExpired("detector", 30),
    ):
        assert detect_document_language(ENGLISH) == "und"
