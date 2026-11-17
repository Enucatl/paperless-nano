"""Recover character semantics only when embedded font data is unambiguous."""

import io
import re
import struct
import unicodedata
import uuid

from fontTools.agl import toUnicode
from fontTools.ttLib import TTFont, TTLibError


def set_font_unicode(document, xref: int, mapping: dict[int, str]) -> None:
    """Attach a one-byte character-to-Unicode map to a simple PDF font."""
    entries = [
        f"<{code:02x}> <{text.encode('utf-16-be').hex()}>"
        for code, text in sorted(mapping.items())
    ]
    cmap = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /RecoveredUnicode def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<00> <ff>\nendcodespacerange\n"
    )
    for offset in range(0, len(entries), 100):
        chunk = entries[offset : offset + 100]
        cmap += f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar\n"
    cmap += "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    stream = document.get_new_xref()
    document.update_object(stream, "<<>>")
    document.update_stream(stream, cmap.encode("ascii"))
    document.xref_set_key(xref, "ToUnicode", f"{stream} 0 R")


def _symbol_characters(font: TTFont) -> dict[int, str]:
    """Read Windows symbol character codes without treating them as Unicode."""
    tables = [t for t in font["cmap"].tables if (t.platformID, t.platEncID) == (3, 0)]
    if len(tables) != 1:
        return {}
    result = {}
    for code, glyph in tables[0].cmap.items():
        if 0xF000 <= code <= 0xF0FF:
            code -= 0xF000
        if glyph == ".notdef":
            continue
        if not 0 <= code <= 255 or code in result and result[code] != glyph:
            return {}
        result[code] = glyph
    return result


def _semantic_text(text: str) -> bool:
    """Reject private-use, control and unassigned values as semantic evidence."""
    return bool(text) and all(
        not unicodedata.category(char).startswith("C") for char in text
    )


def _embedded_mapping(font: TTFont) -> dict[int, str]:
    """Resolve symbol glyphs from real Unicode tables or descriptive glyph names."""
    characters = _symbol_characters(font)
    if not characters:
        return {}
    unicode_by_glyph = {}
    for table in font["cmap"].tables:
        if table.isUnicode():
            for code, glyph in table.cmap.items():
                text = chr(code)
                if _semantic_text(text):
                    unicode_by_glyph.setdefault(glyph, set()).add(text)
    mapping = {}
    named = "post" in font and font["post"].formatType != 3
    for code, glyph in characters.items():
        meanings = unicode_by_glyph.get(glyph, set()).copy()
        if named:
            text = toUnicode(glyph)
            if _semantic_text(text):
                meanings.add(text)
        if len(meanings) != 1:
            return {}
        mapping[code] = meanings.pop()
    return mapping


def _reader_mapping(document, xref: int, font: TTFont) -> dict[int, str]:
    """Preserve MuPDF's consistent guesses for glyphs used by an ambiguous font."""
    codes = {
        code: font.getGlyphID(name) for code, name in _symbol_characters(font).items()
    }
    meanings = {}
    for page in document:
        names = {}
        for reference in page.get_fonts():
            names.setdefault(reference[3].split("+")[-1], set()).add(reference[0])
        for span in page.get_texttrace():
            refs = names.get(span["font"].split("+")[-1], set())
            if xref not in refs:
                continue
            if refs != {xref}:
                return {}
            for value, glyph, _, _ in span["chars"]:
                if not 0 <= value <= 0x10FFFF or value == 0xFFFD:
                    return {}
                text = chr(value)
                if not _semantic_text(text) or glyph not in codes.values():
                    return {}
                if meanings.setdefault(glyph, text) != text:
                    return {}
    return {code: meanings[glyph] for code, glyph in codes.items() if glyph in meanings}


def recover_embedded_font_unicode(document) -> bool:
    """Prefer embedded semantics, then preserve valid source-reader guesses."""
    changed = False
    fonts = {
        font[0]
        for page in document
        for font in page.get_fonts()
        if font[2] == "TrueType"
    }
    for xref in fonts:
        if (
            document.xref_get_key(xref, "ToUnicode")[0] != "null"
            or document.xref_get_key(xref, "Encoding")[0] != "null"
        ):
            continue
        kind, flags = document.xref_get_key(xref, "FontDescriptor/Flags")
        if kind != "int" or not int(flags) & 4 or int(flags) & 32:
            continue
        data = document.extract_font(xref)[3]
        if not data:
            continue
        try:
            with TTFont(io.BytesIO(data)) as font:
                mapping = _embedded_mapping(font) or _reader_mapping(
                    document, xref, font
                )
        except KeyError, ValueError, TTLibError, struct.error:
            continue
        if mapping:
            set_font_unicode(document, xref, mapping)
            changed = True
    return changed


def _font_programs(document, xref: int):
    """Read GID-indexed programs, metrics and composite dependencies from TrueType."""
    if document.xref_get_key(xref, "Type") != ("name", "/Font"):
        return None
    data = document.extract_font(xref)[3]
    if not data:
        return None
    try:
        with TTFont(io.BytesIO(data)) as font:
            glyf = font["glyf"]
            names = font.getGlyphOrder()
            programs = tuple(
                (glyf[name].compile(glyf, recalcBBoxes=False), font["hmtx"][name])
                for name in names
            )
            required = {
                font.getGlyphID(component.glyphName)
                for name in names
                if glyf[name].isComposite()
                for component in glyf[name].components
            }
            return font["head"].unitsPerEm, programs, required
    except KeyError, ValueError, TTLibError, struct.error:
        return None


def _has_identity_codes(document, xref: int) -> bool:
    """Require explicitly unchanged two-byte character-to-glyph identifiers."""
    if document.xref_get_key(xref, "Subtype") != ("name", "/Type0") or (
        document.xref_get_key(xref, "Encoding") != ("name", "/Identity-H")
    ):
        return False
    kind, descendants = document.xref_get_key(xref, "DescendantFonts")
    if kind == "xref":
        descendants = document.xref_object(int(descendants.split()[0]))
    elif kind != "array":
        return False
    match = re.fullmatch(r"\[\s*(\d+) 0 R\s*\]", descendants)
    if not match:
        return False
    descendant = int(match[1])
    return document.xref_get_key(descendant, "Subtype") == (
        "name",
        "/CIDFontType2",
    ) and document.xref_get_key(descendant, "CIDToGIDMap") == ("name", "/Identity")


def _winansi_glyphs(document, xref: int) -> set[int] | None:
    """Bound reachable GIDs only for explicit nonsymbolic WinAnsi TrueType fonts."""
    if document.xref_get_key(xref, "Subtype") != ("name", "/TrueType") or (
        document.xref_get_key(xref, "Encoding") != ("name", "/WinAnsiEncoding")
    ):
        return None
    kind, flags = document.xref_get_key(xref, "FontDescriptor/Flags")
    if kind != "int" or not int(flags) & 32 or int(flags) & 4:
        return None
    try:
        with TTFont(io.BytesIO(document.extract_font(xref)[3])) as font:
            tables = font["cmap"].tables
            if not any(
                table.platformID == 3 and table.platEncID == 1 for table in tables
            ) or any(
                table.platformID == 3 and table.platEncID == 0 for table in tables
            ):
                return None
            unicode_values = {
                ord(bytes([code]).decode("cp1252", errors="replace"))
                for code in range(256)
            } | {0x2022, 0x20, 0x2D}
            names = {font.getGlyphOrder()[0]}
            names.update(
                name
                for table in tables
                if table.isUnicode()
                for code, name in table.cmap.items()
                if code in unicode_values
            )
            # Composite components can be retained without a directly encoded code.
            pending = list(names)
            while pending:
                glyph = font["glyf"][pending.pop()]
                if glyph.isComposite():
                    for component in glyph.components:
                        if component.glyphName not in names:
                            names.add(component.glyphName)
                            pending.append(component.glyphName)
            return {font.getGlyphID(name) for name in names}
    except KeyError, ValueError, TTLibError, struct.error:
        return None


def _used_identity_glyphs(document) -> dict[int, set[int]]:
    """Attribute rendered GIDs using unique font names in an isolated PDF copy."""
    import pymupdf

    parents = {}
    for xref in range(1, document.xref_length()):
        kind, descendants = document.xref_get_key(xref, "DescendantFonts")
        if kind == "xref":
            descendants = document.xref_object(int(descendants.split()[0]))
        elif kind != "array":
            continue
        match = re.fullmatch(r"\[\s*(\d+) 0 R\s*\]", descendants)
        if match:
            parents.setdefault(int(match[1]), set()).add(xref)
    used = {}
    prefix = f"Probe{uuid.uuid4().hex[:16]}_"
    with pymupdf.open(stream=document.tobytes(), filetype="pdf") as probe:
        for xref in range(1, probe.xref_length()):
            if probe.xref_get_key(xref, "Type") != ("name", "/Font"):
                continue
            name = f"/{prefix}{xref}"
            probe.xref_set_key(xref, "BaseFont", name)
            kind, descriptor = probe.xref_get_key(xref, "FontDescriptor")
            if kind == "xref":
                copy = probe.get_new_xref()
                probe.update_object(copy, probe.xref_object(int(descriptor.split()[0])))
                probe.xref_set_key(copy, "FontName", name)
                probe.xref_set_key(xref, "FontDescriptor", f"{copy} 0 R")
        with pymupdf.open(stream=probe.tobytes(), filetype="pdf") as traced:
            for page in traced:
                for span in page.get_texttrace():
                    match = re.fullmatch(prefix + r"(\d+)", span["font"])
                    candidates = parents.get(int(match[1]), set()) if match else set()
                    if len(candidates) == 1:
                        xref = next(iter(candidates))
                        used.setdefault(xref, set()).update(
                            glyph for _, glyph, _, _ in span["chars"] if glyph >= 0
                        )
    return used


def restore_identity_font_unicode(original, converted) -> None:
    """Restore explicit Unicode only for unambiguous, unchanged Identity-H glyphs.

    Compare every retained program and composite dependency at its original GID.
    Removed subset glyphs do not change the source meaning of their unused codes.
    A matching source with unknown encoding or Unicode prevents restoration.
    """
    sources = []
    for xref in range(1, original.xref_length()):
        identity = _font_programs(original, xref)
        if identity is None:
            continue
        cmap = None
        kind, reference = original.xref_get_key(xref, "ToUnicode")
        if _has_identity_codes(original, xref) and kind == "xref":
            cmap_xref = int(reference.split()[0])
            candidate = original.xref_stream(cmap_xref)
            if (
                candidate
                and original.xref_get_key(cmap_xref, "UseCMap")[0] == "null"
                and not re.search(rb"\busecmap\b", candidate)
            ):
                cmap = candidate
        sources.append((identity, cmap, _winansi_glyphs(original, xref)))
    used = (
        _used_identity_glyphs(converted)
        if any(reachable is not None for _, _, reachable in sources)
        else {}
    )
    for xref in range(1, converted.xref_length()):
        if not _has_identity_codes(converted, xref):
            continue
        identity = _font_programs(converted, xref)
        if identity is None:
            continue
        units, programs, required = identity
        retained = {gid for gid, (program, _) in enumerate(programs) if program}
        if not retained:
            continue
        mappings = {
            cmap
            for (source_units, source_programs, _), cmap, reachable in sources
            if units == source_units
            and (reachable is None or used.get(xref, set()) <= reachable)
            and all(
                gid < len(source_programs)
                and gid < len(programs)
                and programs[gid] == source_programs[gid]
                for gid in retained | required
            )
        }
        if len(mappings) != 1 or None in mappings:
            continue
        stream = converted.get_new_xref()
        converted.update_object(stream, "<<>>")
        converted.update_stream(stream, mappings.pop())
        converted.xref_set_key(xref, "ToUnicode", f"{stream} 0 R")
