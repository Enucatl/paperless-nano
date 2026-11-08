"""Detect document language without retaining language profiles in the web process."""

import re
import subprocess
import sys
from pathlib import Path

LANGUAGE_CHOICES = (
    ("af", "Afrikaans"),
    ("am", "Amharic"),
    ("apc", "Levantine Arabic"),
    ("apd", "Sudanese Arabic"),
    ("arb", "Modern Standard Arabic"),
    ("arq", "Algerian Arabic"),
    ("ary", "Moroccan Arabic"),
    ("arz", "Egyptian Arabic"),
    ("bg", "Bulgarian"),
    ("bho", "Bhojpuri"),
    ("bn", "Bengali"),
    ("ca", "Catalan"),
    ("cjy", "Jinyu Chinese"),
    ("cmn", "Mandarin Chinese"),
    ("cs", "Czech"),
    ("cy", "Welsh"),
    ("da", "Danish"),
    ("de", "Standard German"),
    ("el", "Greek"),
    ("en", "English"),
    ("es", "Spanish"),
    ("et", "Estonian"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("gu", "Gujarati"),
    ("ha", "Hausa"),
    ("hak", "Hakka Chinese"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hr", "Croatian"),
    ("hsn", "Xiang Chinese"),
    ("hu", "Hungarian"),
    ("id", "Indonesian"),
    ("it", "Italian"),
    ("ja", "Japanese"),
    ("jv", "Javanese"),
    ("kn", "Kannada"),
    ("ko", "Korean"),
    ("ln", "Lingala"),
    ("lt", "Lithuanian"),
    ("lv", "Latvian"),
    ("mk", "Macedonian"),
    ("ml", "Malayalam"),
    ("mr", "Marathi"),
    ("my", "Burmese"),
    ("nan", "Min Nan Chinese"),
    ("ne", "Nepali"),
    ("nl", "Dutch"),
    ("no", "Norwegian"),
    ("or", "Odia"),
    ("pa", "Eastern Punjabi"),
    ("pcm", "Nigerian Pidgin"),
    ("pes", "Iranian Persian (Farsi)"),
    ("pl", "Polish"),
    ("pnb", "Western Punjabi"),
    ("pt", "Portuguese"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sk", "Slovak"),
    ("sl", "Slovenian"),
    ("so", "Somali"),
    ("sq", "Albanian"),
    ("sv", "Swedish"),
    ("sw", "Swahili"),
    ("ta", "Tamil"),
    ("te", "Telugu"),
    ("th", "Thai"),
    ("tl", "Tagalog"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("und", "Unknown / indeterminate"),
    ("ur", "Urdu"),
    ("vi", "Vietnamese"),
    ("wuu", "Wu Chinese"),
    ("yo", "Yoruba"),
    ("yue", "Yue Chinese (Cantonese)"),
)
LANGUAGE_CODES = frozenset(code for code, _ in LANGUAGE_CHOICES)
LANGUAGE_ALIASES = {"zh-cn": "cmn", "zh-tw": "cmn", "ar": "arb", "fa": "pes"}


def normalize_language(code: str) -> str:
    """Normalize supported detector or legacy codes; reject other values."""
    if not isinstance(code, str):
        raise ValueError("Language must be a supported code")
    code = LANGUAGE_ALIASES.get(code, code)
    if code not in LANGUAGE_CODES:
        raise ValueError("Language must be a supported code")
    return code


def detect_document_language(content: str) -> str:
    """Detect one primary language; short, noisy or unusable text is unknown."""
    letters = [char for char in content if char.isalpha()]
    if (
        len(letters) < 20
        or len(set(letters)) < 5
        or len(letters) < len(content.strip()) * 0.3
    ):
        return "und"
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--document"],
            input=content,
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=30,
        )
        return normalize_language(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return "und"


def detect_language(content: str) -> str:
    """Run the existing detector with request-scoped model residency.

    Text travels through a private pipe, never command arguments or files.
    The caller retains its existing fallback when detection fails.
    """
    if not content:
        return "en"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        input=content,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    language = result.stdout.strip()
    if re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,3})?", language) is None:
        raise ValueError("Invalid language detection result")
    return language


if __name__ == "__main__":
    from langdetect import DetectorFactory
    from langdetect import detect

    if "--document" in sys.argv:
        DetectorFactory.seed = 0
    sys.stdout.write(detect(sys.stdin.buffer.read().decode("utf-8")))
