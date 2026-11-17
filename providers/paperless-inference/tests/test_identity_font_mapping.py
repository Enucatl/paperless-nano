"""Preserve explicit ligature semantics without guessing font identity."""

import hashlib
import io

import pymupdf
import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from paperless_inference.archive import create_searchable_archive
from paperless_inference.font_mapping import restore_identity_font_unicode


def _font_bytes(composite=False):
    """Build a tiny original font whose two visible glyphs have distinct outlines."""
    builder = FontBuilder(1000, isTTF=True)
    names = [".notdef", "fi", "fl", "unused"]
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({0xFB01: "fi", 0xFB02: "fl"})
    glyphs = {}
    for index, name in enumerate(names):
        pen = TTGlyphPen(dict.fromkeys(names))
        if composite and name == "fi":
            pen.addComponent("unused", (1, 0, 0, 1, 0, 0))
        elif index:
            pen.moveTo((0, 0))
            pen.lineTo((index * 100, 0))
            pen.lineTo((index * 100, 700))
            pen.lineTo((0, 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (500, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Synthetic", "styleName": "Regular"})
    builder.setupOS2(
        sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200
    )
    builder.setupPost()
    builder.setupMaxp()
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def _document(composite=False):
    """Create a PDF with explicit multi-codepoint fi/fl mappings."""
    document = pymupdf.open()
    page = document.new_page()
    xref = page.insert_font(fontname="Synthetic", fontbuffer=_font_bytes(composite))
    page.insert_text((20, 30), "\ufb01\ufb02", fontname="Synthetic")
    descendant = int(
        document.xref_get_key(xref, "DescendantFonts")[1].strip("[]").split()[0]
    )
    document.xref_set_key(descendant, "CIDToGIDMap", "/Identity")
    cmap_xref = int(document.xref_get_key(xref, "ToUnicode")[1].split()[0])
    document.update_stream(
        cmap_xref,
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Synthetic def /CMapType 2 def\n"
        b"1 begincodespacerange <0000> <ffff> endcodespacerange\n"
        b"1 beginbfrange <0001> <0002> [<00660069> <0066006c>] endbfrange\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end",
    )
    return document, xref, cmap_xref


def test_ligature_unicode_survives_real_conversion(tmp_path):
    """Ghostscript and veraPDF preserve fi/fl sequences and the visible page."""
    original, _, _ = _document()
    source = tmp_path / "source.pdf"
    destination = tmp_path / "archive.pdf"
    with original:
        original.save(source)
    before = source.read_bytes()
    create_searchable_archive(
        source,
        {
            "source_sha256": hashlib.sha256(before).hexdigest(),
            "page_count": 1,
            "pages": [{"page_index": 0}],
        },
        destination,
    )
    assert source.read_bytes() == before
    with pymupdf.open(source) as original, pymupdf.open(destination) as archive:
        assert original[0].get_text().strip() == "fifl"
        assert archive[0].get_text() == original[0].get_text()
        assert archive[0].get_pixmap().samples == original[0].get_pixmap().samples


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "encoding",
        "cidmap",
        "glyph",
        "ambiguous",
        "missing_map",
        "source_encoding",
        "usecmap",
    ],
)
def test_repair_requires_unambiguous_identity(failure):
    """Only proven identical fonts may receive the original explicit mapping."""
    original, xref, cmap_xref = _document()
    with original, pymupdf.open(stream=original.tobytes(), filetype="pdf") as converted:
        expected = original.xref_stream(cmap_xref)
        converted.update_stream(cmap_xref, b"broken mapping")
        if failure == "encoding":
            converted.xref_set_key(xref, "Encoding", "/Identity-V")
        elif failure == "cidmap":
            descendant = int(
                converted.xref_get_key(xref, "DescendantFonts")[1]
                .strip("[]")
                .split()[0]
            )
            converted.xref_set_key(descendant, "CIDToGIDMap", "null")
        elif failure == "glyph":
            descendant = int(
                converted.xref_get_key(xref, "DescendantFonts")[1]
                .strip("[]")
                .split()[0]
            )
            font_xref = int(
                converted.xref_get_key(descendant, "FontDescriptor/FontFile2")[
                    1
                ].split()[0]
            )
            from fontTools.ttLib import TTFont

            with TTFont(io.BytesIO(converted.xref_stream(font_xref))) as font:
                font["glyf"][font.getGlyphOrder()[1]].coordinates[1] = (123, 0)
                buffer = io.BytesIO()
                font.save(buffer)
            converted.update_stream(font_xref, buffer.getvalue())
        elif failure in {"ambiguous", "missing_map", "source_encoding"}:
            duplicate = original.get_new_xref()
            original.update_object(duplicate, original.xref_object(xref))
            other_map = original.get_new_xref()
            original.update_object(other_map, "<<>>")
            original.update_stream(
                other_map, expected.replace(b"00660069", b"0066006c")
            )
            original.xref_set_key(duplicate, "ToUnicode", f"{other_map} 0 R")
            if failure == "missing_map":
                original.xref_set_key(duplicate, "ToUnicode", "null")
            elif failure == "source_encoding":
                original.xref_set_key(duplicate, "Encoding", "/Identity-V")
        elif failure == "usecmap":
            original.xref_set_key(cmap_xref, "UseCMap", "/Other")
        restore_identity_font_unicode(original, converted)
        result = int(converted.xref_get_key(xref, "ToUnicode")[1].split()[0])
        assert converted.xref_stream(result) == (
            b"broken mapping" if failure else expected
        )


@pytest.mark.parametrize("composite", [False, True])
def test_subset_may_drop_unused_glyph_but_not_composite_dependency(composite):
    """A missing unused outline is harmless; a missing component changes a glyph."""
    from fontTools.ttLib import TTFont
    from fontTools.ttLib.tables._g_l_y_f import Glyph

    original, xref, cmap_xref = _document(composite)
    with original, pymupdf.open(stream=original.tobytes(), filetype="pdf") as converted:
        expected = original.xref_stream(cmap_xref)
        converted.update_stream(cmap_xref, b"broken mapping")
        descendant = int(
            converted.xref_get_key(xref, "DescendantFonts")[1].strip("[]").split()[0]
        )
        font_xref = int(
            converted.xref_get_key(descendant, "FontDescriptor/FontFile2")[1].split()[0]
        )
        with TTFont(
            io.BytesIO(converted.xref_stream(font_xref)), recalcBBoxes=False
        ) as font:
            font["glyf"][font.getGlyphOrder()[3]] = Glyph()
            buffer = io.BytesIO()
            font.save(buffer)
        converted.update_stream(font_xref, buffer.getvalue())
        restore_identity_font_unicode(original, converted)
        result = int(converted.xref_get_key(xref, "ToUnicode")[1].split()[0])
        assert converted.xref_stream(result) == (
            b"broken mapping" if composite else expected
        )


def test_indirect_descendants_preserve_identity():
    """An indirect descendant array has the same meaning as an inline array."""
    original, xref, cmap_xref = _document()
    with original:
        descendants = original.xref_get_key(xref, "DescendantFonts")[1]
        reference = original.get_new_xref()
        original.update_object(reference, descendants)
        original.xref_set_key(xref, "DescendantFonts", f"{reference} 0 R")
        with pymupdf.open(stream=original.tobytes(), filetype="pdf") as converted:
            expected = original.xref_stream(cmap_xref)
            converted.update_stream(cmap_xref, b"broken mapping")
            restore_identity_font_unicode(original, converted)
            result = int(converted.xref_get_key(xref, "ToUnicode")[1].split()[0])
            assert converted.xref_stream(result) == expected


@pytest.mark.parametrize(
    "case",
    [
        "unreachable",
        "custom_encoding",
        "symbolic",
        "reachable",
        "unused_only",
        "missing_windows_cmap",
        "alternate_cmap",
        "shared_descendant",
    ],
)
def test_simple_font_exclusion_requires_proven_unreachable_used_glyphs(case):
    """Only inaccessible used glyphs can rule out a competing simple source font."""
    from fontTools.ttLib import TTFont

    original, xref, cmap_xref = _document()
    with original:
        descendant = int(
            original.xref_get_key(xref, "DescendantFonts")[1].strip("[]").split()[0]
        )
        descriptor = int(
            original.xref_get_key(descendant, "FontDescriptor")[1].split()[0]
        )
        if case in {
            "reachable",
            "unused_only",
            "missing_windows_cmap",
            "alternate_cmap",
        }:
            font_xref = int(
                original.xref_get_key(descriptor, "FontFile2")[1].split()[0]
            )
            with TTFont(io.BytesIO(original.xref_stream(font_xref))) as font:
                if case == "missing_windows_cmap":
                    font["cmap"].tables = [
                        table for table in font["cmap"].tables if table.platformID != 3
                    ]
                for table in font["cmap"].tables:
                    if case == "alternate_cmap" and table.platformID == 3:
                        continue
                    if table.isUnicode():
                        table.cmap.update({65: "fi", 66: "fl"})
                        if case in {"reachable", "alternate_cmap"}:
                            table.cmap[67] = "unused"
                buffer = io.BytesIO()
                font.save(buffer)
            original.update_stream(font_xref, buffer.getvalue())
        simple_descriptor = original.get_new_xref()
        original.update_object(simple_descriptor, original.xref_object(descriptor))
        original.xref_set_key(
            simple_descriptor, "Flags", "4" if case == "symbolic" else "32"
        )
        simple = original.get_new_xref()
        original.update_object(
            simple,
            f"<< /Type /Font /Subtype /TrueType /BaseFont /SyntheticSimple "
            f"/Encoding /WinAnsiEncoding /FontDescriptor {simple_descriptor} 0 R >>",
        )
        if case == "custom_encoding":
            original.xref_set_key(
                simple,
                "Encoding",
                "<< /BaseEncoding /WinAnsiEncoding /Differences [65 /fi] >>",
            )
        if case == "shared_descendant":
            duplicate = original.get_new_xref()
            original.update_object(duplicate, original.xref_object(xref))
        with pymupdf.open(stream=original.tobytes(), filetype="pdf") as converted:
            expected = original.xref_stream(cmap_xref)
            converted.update_stream(cmap_xref, b"broken mapping")
            restore_identity_font_unicode(original, converted)
            result = int(converted.xref_get_key(xref, "ToUnicode")[1].split()[0])
            assert converted.xref_stream(result) == (
                expected if case == "unreachable" else b"broken mapping"
            )


def test_used_glyph_probe_preserves_source_objects():
    """Temporary font names cannot alter the PDF being repaired."""
    from paperless_inference.font_mapping import _used_identity_glyphs

    document, xref, _ = _document()
    with document:
        before = [document.xref_object(i) for i in range(1, document.xref_length())]
        assert _used_identity_glyphs(document) == {xref: {1, 2}}
        assert [
            document.xref_object(i) for i in range(1, document.xref_length())
        ] == before
