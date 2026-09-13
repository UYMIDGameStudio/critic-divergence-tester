"""Real ZIP/XML fixtures exercise languages, relationships and resource bounds."""
from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape

from document_review_ingest import IngestionError, IngestionLimits
from document_review_model import RawFileBinding
from document_review_office_formats import A, OFFICE, P, PKG, R, S, TABLE, TEXT, parse_office

LANGUAGES = ["English", "简体中文", "繁體中文", "Straße für Grüße", "École française", "日本語かなカナ", "Русский язык", "Lingua Latīna æ œ"]
MULTILINGUAL = " | ".join(LANGUAGES)


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def relationships(items):
    return f'<Relationships xmlns="{PKG}">' + "".join(f'<Relationship Id="{identifier}" Type="{R}/{kind}" Target="{escape(target)}"{(" TargetMode=" + chr(34) + mode + chr(34)) if mode else ""}/>' for identifier, kind, target, mode in items) + '</Relationships>'


def xlsx_entries():
    return {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        "_rels/.rels": relationships([("main", "officeDocument", "xl/workbook.xml", None)]),
        "xl/workbook.xml": f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets><sheet name="第二份" sheetId="2" r:id="second"/><sheet name="First" state="hidden" sheetId="1" r:id="first"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": relationships([("first", "worksheet", "worksheets/one.xml", None), ("strings", "sharedStrings", "sharedStrings.xml", None), ("second", "worksheet", "worksheets/two.xml", None)]),
        "xl/sharedStrings.xml": f'<sst xmlns="{S}" count="3" uniqueCount="2"><si><r><t>{escape(MULTILINGUAL)}</t></r><rPh sb="0" eb="1"><t>PHONETIC-ANNOTATION</t></rPh></si><si><t>Hidden value</t></si></sst>',
        "xl/worksheets/one.xml": f'<worksheet xmlns="{S}"><sheetData><row r="3"><c r="B3" t="s"><v>1</v></c></row></sheetData></worksheet>',
        "xl/worksheets/two.xml": f'<worksheet xmlns="{S}"><dimension ref="A1:XFD1048576"/><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>{escape(MULTILINGUAL)}</t></is></c></row><row r="2"><c r="B2"><f>SUM(1,2)</f><v>3</v></c><c r="C2"><f>EXTERNAL_CALL()</f></c><c r="D2" t="b"><v>1</v></c><c r="E2" t="e"><v>#DIV/0!</v></c><c r="F2" t="str"><f>TEXT_RESULT()</f><v>{escape(MULTILINGUAL)}</v></c></row></sheetData></worksheet>',
    }


def odt_entries(content=None):
    if content is None:
        content = f'<text:h text:outline-level="2">{escape(MULTILINGUAL)}</text:h><text:p>Before<text:s text:c="2"/>middle<text:tab/>after<text:line-break/>{escape(MULTILINGUAL)}</text:p><text:list><text:list-item><text:p>{escape(MULTILINGUAL)}</text:p><text:list><text:list-item><text:p>Nested</text:p></text:list-item></text:list></text:list-item></text:list><table:table table:name="资料"><table:table-column table:number-columns-repeated="2"/><table:table-header-rows><table:table-row table:number-rows-repeated="2"><table:table-cell><text:p>{escape(MULTILINGUAL)}</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="2" table:formula="of:=1+1"/></table:table-row></table:table-header-rows></table:table>'
    root = f'<office:document-content xmlns:office="{OFFICE}" xmlns:text="{TEXT}" xmlns:table="{TABLE}"><office:body><office:text>{content}</office:text></office:body></office:document-content>'
    return {"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": root,
            "META-INF/manifest.xml": '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/></manifest:manifest>'}


def shape(text, *, title=False, bullet=False):
    placeholder = '<p:ph type="title"/>' if title else ''
    props = '<a:pPr lvl="1"><a:buChar char="•"/></a:pPr>' if bullet else ''
    return f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="Text"/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p>{props}<a:r><a:t>{escape(text)}</a:t></a:r></a:p></p:txBody></p:sp>'


def pptx_entries():
    table = f'<p:graphicFrame><a:graphic><a:graphicData uri="{A}/table"><a:tbl><a:tblGrid><a:gridCol w="100"/><a:gridCol w="100"/></a:tblGrid><a:tr h="100"><a:tc><a:txBody><a:p><a:r><a:t>{escape(MULTILINGUAL)}</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>One</a:t></a:r><a:br/><a:r><a:t>Two</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    picture = f'<p:pic><p:nvPicPr><p:cNvPr id="3" name="Picture" descr="{escape(MULTILINGUAL)}"/></p:nvPicPr></p:pic>'
    return {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
        "_rels/.rels": relationships([("main", "officeDocument", "ppt/presentation.xml", None)]),
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="257" r:id="second"/><p:sldId id="256" r:id="first"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": relationships([("first", "slide", "slides/slide1.xml", None), ("second", "slide", "slides/slide2.xml", None)]),
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}" show="0"><p:cSld><p:spTree>{shape("Last slide " + MULTILINGUAL)}</p:spTree></p:cSld></p:sld>',
        "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{shape(MULTILINGUAL, title=True)}<p:grpSp>{shape(MULTILINGUAL, bullet=True)}</p:grpSp>{table}{picture}</p:spTree></p:cSld></p:sld>',
        "ppt/slides/_rels/slide2.xml.rels": relationships([("notes", "notesSlide", "../notesSlides/notes2.xml", None)]),
        "ppt/notesSlides/notes2.xml": f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>{shape(MULTILINGUAL)}<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>UNWANTED SLIDE NUMBER</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>',
        "ppt/slides/unreferenced.xml": '<malformed-unused-part>',
    }


class OfficeFormatsTests(unittest.TestCase):
    def parse(self, extension, entries, **overrides):
        raw = zipped(entries)
        source = RawFileBinding("多语言资料" + extension, extension, "application/octet-stream", len(raw), hashlib.sha256(raw).hexdigest(), "source/original" + extension)
        limits = SimpleNamespace(**{**asdict(IngestionLimits()), **overrides})
        with patch("urllib.request.urlopen", side_effect=AssertionError("network access forbidden")), patch("subprocess.run", side_effect=AssertionError("Office process execution forbidden")):
            result = parse_office(raw, source, limits)
        self.assertEqual(result.source, source)
        self.assertTrue(result.metadata["extraction_only"])
        self.assertFalse(result.metadata["original_format_export_supported"])
        self.assertFalse(result.metadata["macros_executed"])
        self.assertFalse(result.metadata["external_links_fetched"])
        self.assertTrue(result.quality.requires_confirmation)
        self.assertEqual(len({block.block_id for block in result.blocks}), len(result.blocks))
        return result

    def test_xlsx_order_coordinates_shared_inline_and_cached_multilingual_text(self):
        document = self.parse(".xlsx", xlsx_entries())
        self.assertEqual(document.metadata["sheet_order"], ["第二份", "First"])
        self.assertEqual([block.text for block in document.blocks if block.kind == "heading"], ["第二份", "First"])
        cells = [block for block in document.blocks if block.kind == "table_cell" and block.attrs["sheet_name"] == "第二份"]
        lookup = {(block.location.row, block.location.column): block for block in cells}
        for coordinate in [(0, 0), (0, 2), (1, 5)]:
            self.assertEqual(lookup[coordinate].text, MULTILINGUAL)
            for language in LANGUAGES:
                self.assertIn(language, lookup[coordinate].text)
        self.assertEqual(lookup[(1, 1)].text, "3")
        self.assertEqual(lookup[(1, 2)].text, "")
        self.assertFalse(lookup[(1, 2)].attrs["formula_cache_present"])
        self.assertEqual(lookup[(1, 3)].text, "TRUE")
        self.assertEqual(lookup[(1, 4)].text, "#DIV/0!")
        warning = next(item for item in document.warnings if item.code == "formula-cache-missing")
        self.assertEqual((warning.location.row, warning.location.column), (1, 2))
        self.assertNotIn("PHONETIC-ANNOTATION", document.plain_text)
        hidden = next(block for block in document.blocks if block.text == "Hidden value")
        self.assertEqual((hidden.location.row, hidden.location.column), (2, 1))
        self.assertTrue(hidden.attrs["hidden"])
        self.assertLess(len(document.blocks), 30, "declared XFD1048576 dimension must not allocate empty grid")

    def test_xlsm_macros_external_links_and_formulas_are_never_executed(self):
        entries = xlsx_entries()
        entries["xl/vbaProject.bin"] = b"do not execute this macro"
        entries["xl/worksheets/_rels/two.xml.rels"] = relationships([("link", "hyperlink", "https://example.invalid/never-fetch", "External")])
        document = self.parse(".xlsm", entries)
        codes = {warning.code for warning in document.warnings}
        self.assertIn("macros-not-executed", codes)
        self.assertIn("external-links-not-fetched", codes)
        self.assertIn("formula-cache-only", codes)
        self.assertFalse(document.metadata["formulas_recalculated"])

    def test_excel_escaped_unicode_surrogates_and_literal_escapes(self):
        entries = xlsx_entries()
        entries["xl/sharedStrings.xml"] = f'<sst xmlns="{S}"><si><t>_x00DF_ _x65E5_ _xD83D__xDE00_ _x005F_x0041_</t></si><si><t>hidden</t></si></sst>'
        document = self.parse(".xlsx", entries)
        self.assertTrue(any(block.text == "ß 日 😀 _x0041_" for block in document.blocks))

    def test_xlsx_resource_limits_reject_sparse_cells_and_total_grid_growth(self):
        for overrides in [{"max_office_cells": 5}, {"max_office_sheets": 1}, {"max_office_text_chars": 30}]:
            with self.subTest(overrides=overrides), self.assertRaises(IngestionError):
                self.parse(".xlsx", xlsx_entries(), **overrides)
        for reference in ["A1048576", "XFD1", "A0", "../A1"]:
            entries = xlsx_entries()
            row_number = "1048576" if reference == "A1048576" else "1"
            entries["xl/worksheets/two.xml"] = f'<worksheet xmlns="{S}"><sheetData><row r="{row_number}"><c r="{reference}"><v>1</v></c></row></sheetData></worksheet>'
            with self.subTest(reference=reference), self.assertRaises(IngestionError):
                self.parse(".xlsx", entries)

    def test_xlsx_invalid_shared_strings_duplicate_cells_and_rows_fail(self):
        for body in ['<row r="1"><c r="A1" t="s"><v>999</v></c></row>', '<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>', '<row r="1"/><row r="1"/>', '<row r="1"><c r="B2"><v>1</v></c></row>']:
            entries = xlsx_entries()
            entries["xl/worksheets/two.xml"] = f'<worksheet xmlns="{S}"><sheetData>{body}</sheetData></worksheet>'
            with self.subTest(body=body), self.assertRaises(IngestionError):
                self.parse(".xlsx", entries)

    def test_odt_multilingual_headings_lists_tables_and_inline_whitespace(self):
        document = self.parse(".odt", odt_entries())
        heading = next(block for block in document.blocks if block.kind == "heading")
        self.assertEqual(heading.text, MULTILINGUAL)
        self.assertEqual(heading.level, 2)
        paragraph = next(block for block in document.blocks if block.kind == "paragraph")
        self.assertEqual(paragraph.text, "Before  middle\tafter\n" + MULTILINGUAL)
        listing = [block for block in document.blocks if block.kind == "list_item"]
        self.assertEqual([block.attrs["list_level"] for block in listing], [0, 1])
        self.assertEqual(listing[0].text, MULTILINGUAL)
        table = next(block for block in document.blocks if block.kind == "table")
        self.assertEqual(table.attrs["rows"], [[MULTILINGUAL, "2"], [MULTILINGUAL, "2"]])
        for block in document.blocks:
            if block.kind == "table_cell" and block.location.column == 0:
                self.assertEqual(block.text, MULTILINGUAL)

    def test_odt_utf16_multilingual_xml_and_missing_formula_cache(self):
        entries = odt_entries(f'<text:p>{escape(MULTILINGUAL)}</text:p><table:table><table:table-row><table:table-cell table:formula="of:=SOMETHING()"/></table:table-row></table:table>')
        entries["content.xml"] = ('<?xml version="1.0" encoding="UTF-16"?>' + entries["content.xml"]).encode("utf-16")
        document = self.parse(".odt", entries)
        self.assertEqual(document.blocks[0].text, MULTILINGUAL)
        self.assertEqual(next(block for block in document.blocks if block.kind == "table_cell").text, "")
        self.assertIn("formula-cache-missing", {warning.code for warning in document.warnings})

    def test_odt_repeated_row_column_space_and_total_text_bombs_are_rejected(self):
        examples = [
            '<table:table><table:table-row table:number-rows-repeated="100000000"><table:table-cell/></table:table-row></table:table>',
            '<table:table><table:table-row><table:table-cell table:number-columns-repeated="100000000"/></table:table-row></table:table>',
            '<table:table><table:table-column table:number-columns-repeated="100000000"/></table:table>',
            '<text:p>A<text:s text:c="100000000"/>B</text:p>',
            '<text:p>A<text:s text:c="100"/><text:s text:c="100"/>B</text:p>',
        ]
        for content in examples:
            with self.subTest(content=content), self.assertRaises(IngestionError):
                self.parse(".odt", odt_entries(content), max_office_text_chars=150)
        with self.assertRaises(IngestionError):
            self.parse(".odt", odt_entries(), max_office_cells=3)

    def test_odt_encryption_deep_xml_and_dtd_are_rejected(self):
        encrypted = odt_entries()
        encrypted["META-INF/manifest.xml"] = '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry><manifest:encryption-data/></manifest:file-entry></manifest:manifest>'
        with self.assertRaisesRegex(IngestionError, "加密"):
            self.parse(".odt", encrypted)
        deep = odt_entries('<text:p>' + '<text:span>' * 140 + 'text' + '</text:span>' * 140 + '</text:p>')
        with self.assertRaisesRegex(IngestionError, "嵌套深度"):
            self.parse(".odt", deep)
        dtd = odt_entries()
        dtd["content.xml"] = '<!DOCTYPE document [<!ENTITY x SYSTEM "file:///secret">]>' + dtd["content.xml"]
        with self.assertRaises(IngestionError):
            self.parse(".odt", dtd)

    def test_pptx_uses_presentation_order_and_retains_languages_in_all_text_roles(self):
        document = self.parse(".pptx", pptx_entries())
        self.assertEqual(document.metadata["slide_order"], ["ppt/slides/slide2.xml", "ppt/slides/slide1.xml"])
        self.assertEqual(document.quality.page_count, 2)
        self.assertEqual(document.blocks[0].text, MULTILINGUAL)
        self.assertEqual(document.blocks[0].kind, "heading")
        self.assertTrue(all(block.location.page == 1 for block in document.blocks[:-1]))
        self.assertEqual(document.blocks[-1].location.page, 2)
        self.assertTrue(document.blocks[-1].attrs["hidden"])
        listing = next(block for block in document.blocks if block.kind == "list_item")
        self.assertEqual(listing.text, MULTILINGUAL)
        self.assertEqual(listing.attrs["list_level"], 1)
        table = next(block for block in document.blocks if block.kind == "table")
        self.assertEqual(table.attrs["rows"], [[MULTILINGUAL, "One\nTwo"]])
        notes = next(block for block in document.blocks if block.attrs.get("role") == "speaker-notes")
        self.assertEqual(notes.text, "[演讲者备注] " + MULTILINGUAL)
        self.assertNotIn("UNWANTED SLIDE NUMBER", document.plain_text)
        image = next(block for block in document.blocks if block.kind == "image_placeholder")
        self.assertEqual(image.attrs["description"], MULTILINGUAL)

    def test_pptm_macro_bytes_are_ignored_and_slide_limits_are_enforced(self):
        entries = pptx_entries()
        entries["ppt/vbaProject.bin"] = b"never executed"
        document = self.parse(".pptm", entries)
        self.assertIn("macros-not-executed", {warning.code for warning in document.warnings})
        with self.assertRaises(IngestionError):
            self.parse(".pptx", pptx_entries(), max_office_slides=1)
        with self.assertRaises(IngestionError):
            self.parse(".pptx", pptx_entries(), max_office_cells=1)

    def test_required_relationships_cannot_fetch_external_or_escape_package(self):
        for target, mode in [("https://example.invalid/never-fetch", "External"), ("../../../outside.xml", None), ("..%2f..%2f..%2foutside.xml", None)]:
            entries = pptx_entries()
            entries["ppt/_rels/presentation.xml.rels"] = relationships([("first", "slide", "slides/slide1.xml", None), ("second", "slide", target, mode)])
            with self.subTest(target=target), self.assertRaises(IngestionError):
                self.parse(".pptx", entries)

    def test_missing_duplicate_and_broken_office_relationships_fail_explicitly(self):
        missing = xlsx_entries()
        del missing["xl/worksheets/two.xml"]
        with self.assertRaisesRegex(IngestionError, "缺少"):
            self.parse(".xlsx", missing)
        duplicate = pptx_entries()
        duplicate["ppt/_rels/presentation.xml.rels"] = relationships([("second", "slide", "slides/slide2.xml", None), ("second", "slide", "slides/slide1.xml", None)])
        with self.assertRaisesRegex(IngestionError, "重复"):
            self.parse(".pptx", duplicate)
        for extension, entries in [(".xlsx", xlsx_entries()), (".pptx", pptx_entries())]:
            del entries["_rels/.rels"]
            with self.subTest(extension=extension), self.assertRaises(IngestionError):
                self.parse(extension, entries)

    def test_strict_office_namespaces_extract_same_unicode_text(self):
        replacements = {S: "http://purl.oclc.org/ooxml/spreadsheetml/main", P: "http://purl.oclc.org/ooxml/presentationml/main", A: "http://purl.oclc.org/ooxml/drawingml/main", R: "http://purl.oclc.org/ooxml/officeDocument/relationships"}
        for extension, entries in [(".xlsx", xlsx_entries()), (".pptx", pptx_entries())]:
            for name, value in entries.items():
                for before, after in replacements.items():
                    value = value.replace(before, after)
                entries[name] = value
            with self.subTest(extension=extension):
                self.assertIn(MULTILINGUAL, self.parse(extension, entries).plain_text)

    def test_source_binding_mismatch_is_rejected(self):
        raw = zipped(odt_entries())
        source = RawFileBinding("file.odt", ".odt", "application/octet-stream", len(raw), "0" * 64, "source/file.odt")
        with self.assertRaisesRegex(IngestionError, "绑定"):
            parse_office(raw, source, IngestionLimits())


if __name__ == "__main__":
    unittest.main()
