"""Content and hostile-input regression for real CSV/HTML/RTF parsing."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from document_review_ingest import IngestionError, IngestionLimits
from document_review_model import RawFileBinding
from document_review_text_formats import parse_text_format


LANGUAGES = (
    "English: clear evidence.",
    "简体中文：证据与论证。",
    "繁體中文：證據與論證。",
    "Deutsch: Größe, äußere Maßnahmen, Straße.",
    "Français : cœur, œuvre, naïveté, français.",
    "日本語：ひらがな、カタカナ、論証。",
    "Русский: доказательство, ёжик, Москва.",
    "Latīna: Rōma, cælum, œconomia, ūnus.",
)


def parse(extension, data, *, encoding=None, limits=None):
    if isinstance(data, str):
        data = data.encode("utf-8")
    source = RawFileBinding("sample" + extension, extension, "application/octet-stream", len(data),
                            hashlib.sha256(data).hexdigest(), "source/sample" + extension)
    return parse_text_format(data, source, limits or IngestionLimits(), encoding=encoding)


def rtf_unicode(text):
    units = text.encode("utf-16-le")
    values = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    return "".join("\\u" + str(value if value < 32768 else value - 65536) + "?" for value in values)


def hex_run(text, encoding):
    return b"".join(("\\'%02x" % byte).encode("ascii") for byte in text.encode(encoding))


class DelimitedTextTests(unittest.TestCase):
    def test_real_padding_limit_blocks_extraction_but_keeps_original_project(self):
        from document_review_studio import DocumentReviewProject
        raw = (",".join(["x"] * 1000) + "\n" + "short\n" * 50).encode()
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename="ragged.csv", content=raw)
            self.assertEqual(project.state()["extraction_state"], "blocked")
            self.assertIn("补齐", project.state()["diagnostics"][0])
            self.assertEqual((project.root / "source/ragged.csv").read_bytes(), raw)
            self.assertFalse((project.root / "extraction/document.json").exists())
            self.assertEqual(project.integrity_errors(), [])

    def test_ragged_table_padding_is_bounded_in_both_row_orders(self):
        for extension, separator in ((".csv", ","), (".tsv", "\t")):
            rows = [separator.join(["wide"] * 4), "one", "two", "three"]
            for ordered in (rows, list(reversed(rows))):
                with self.subTest(extension=extension, rows=ordered), patch("document_review_text_formats.MAX_BLOCKS", 12):
                    with self.assertRaisesRegex(IngestionError, "补齐.*安全上限"):
                        parse(extension, "\n".join(ordered))

    def test_ragged_padding_at_limit_preserves_cells_and_locations(self):
        with patch("document_review_text_formats.MAX_BLOCKS", 12):
            document = parse(".csv", "a,b,c,d\nsecond\nlast")
        self.assertEqual(document.blocks[0].attrs["rows"], [["a", "b", "c", "d"], ["second", "", "", ""], ["last", "", "", ""]])
        last = next(block for block in document.blocks if block.text == "last")
        self.assertEqual((last.location.row, last.location.column), (2, 0))

    def test_csv_quotes_multiline_and_stable_cell_locations(self):
        raw = b'name,note,formula\r\n"Doe, Jane","first\r\nsecond ""quoted""",=1+1\r\nlast,,42\r\n'
        document = parse(".csv", raw)
        again = parse(".csv", raw)
        self.assertEqual(document.to_dict(), again.to_dict())
        table = next(block for block in document.blocks if block.kind == "table")
        self.assertEqual(table.attrs["rows"][1], ["Doe, Jane", 'first\r\nsecond "quoted"', "=1+1"])
        cell = next(block for block in document.blocks if block.text == 'first\r\nsecond "quoted"')
        self.assertEqual((cell.location.row, cell.location.column), (1, 1))
        self.assertEqual(cell.location.table_id, table.block_id)
        self.assertEqual(cell.attrs["source_line"], 2)
        last = next(block for block in document.blocks if block.text == "last")
        self.assertEqual(last.attrs["source_line"], 4)
        self.assertEqual(document.quality.tables_parsed, 1)

    def test_tsv_quoted_tabs_ragged_rows_and_blank_cells(self):
        document = parse(".tsv", 'a\tb\n"内含\t制表"\t"多行\n文本"\nsolo\n')
        table = document.blocks[0]
        self.assertEqual(table.attrs["rows"], [["a", "b"], ["内含\t制表", "多行\n文本"], ["solo", ""]])
        self.assertIn("delimited-ragged-rows", {w.code for w in document.warnings})

    def test_eight_languages_unicode_and_legacy_encodings(self):
        text = "\n".join('"' + value + '"' for value in LANGUAGES)
        for codec in ("utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(codec=codec):
                document = parse(".csv", text.encode(codec))
                self.assertEqual([b.text for b in document.blocks if b.kind == "table_cell"], list(LANGUAGES))
        for codec, value in (("gb18030", LANGUAGES[1]), ("big5", LANGUAGES[2]), ("cp1251", LANGUAGES[6]),
                             ("cp932", LANGUAGES[5]), ("cp1252", LANGUAGES[3]), ("iso8859-15", LANGUAGES[4])):
            with self.subTest(codec=codec):
                document = parse(".tsv", value.encode(codec), encoding=codec)
                self.assertEqual(document.blocks[1].text, value)

    def test_malformed_delimited_text_and_limits_are_rejected(self):
        for text in ('a,"unclosed', '"closed"junk,next'):
            with self.subTest(text=text), self.assertRaises(IngestionError):
                parse(".csv", text)
        with self.assertRaises(IngestionError):
            parse(".csv", b"too big", limits=replace(IngestionLimits(), max_file_bytes=3))
        with self.assertRaises(IngestionError):
            parse(".tsv", "\t".join("x" for _ in range(1001)))

    def test_ambiguous_encoding_is_reported_and_can_be_explicitly_selected(self):
        raw = "中文".encode("big5")
        automatic = parse(".csv", raw)
        self.assertTrue(automatic.metadata["encoding_ambiguous"])
        self.assertIn("text-encoding-ambiguous", {w.code for w in automatic.warnings})
        selected = parse(".csv", raw, encoding="big5")
        self.assertEqual(selected.plain_text, "中文")
        self.assertFalse(selected.metadata["encoding_ambiguous"])


class HTMLTextTests(unittest.TestCase):
    def test_sparse_spans_and_multiple_tables_share_expanded_cell_limit(self):
        sparse = '<table><tr><td rowspan="4">tall</td><td colspan="3">wide</td></tr></table>'
        table = '<table><tr><td colspan="7">text</td></tr></table>'
        for html in (sparse, table * 2):
            with self.subTest(html=html), patch("document_review_text_formats.MAX_BLOCKS", 12):
                with self.assertRaisesRegex(IngestionError, "补齐.*安全上限"):
                    parse(".html", html)
        with patch("document_review_text_formats.MAX_BLOCKS", 14):
            document = parse(".html", table * 2)
        self.assertEqual([b.attrs["rows"] for b in document.blocks if b.kind == "table"], [[["text"] + [""] * 6]] * 2)

    def test_titles_paragraphs_lists_tables_and_no_active_content(self):
        html = '''<!doctype html><html><head><title>Review &amp; evidence</title>
        <style>body:after { content: "DO NOT EXTRACT" }</style></head><body>
        <h1>Heading</h1><p>Hello <b>world</b> &amp; readers<br>Second line.</p>
        <ol><li>First</li><li>Second</li></ol><ul><li>Bullet</li></ul>
        <script>fetch("https://invalid.example/"); EVIL SCRIPT</script>
        <template><title>Hidden title</title><p>HIDDEN TEMPLATE</p></template>
        <div hidden>HIDDEN ATTRIBUTE</div><div aria-hidden="true">HIDDEN ARIA</div>
        <div style="display: /**/none !important">HIDDEN STYLE</div>
        <table><caption>Evidence table</caption><tr><th>Name</th><th>Value</th></tr>
        <tr><td>A</td><td>1 &lt; 2</td></tr></table>
        <img src="https://invalid.example/photo" alt="Evidence image">
        <iframe src="https://invalid.example/">EMBEDDED CONTENT</iframe></body></html>'''
        with patch("socket.create_connection", side_effect=AssertionError("Network access is forbidden")):
            document = parse(".html", html)
        self.assertEqual(document.title, "Review & evidence")
        self.assertIn("Hello world & readers\nSecond line.", document.plain_text)
        self.assertNotIn("HIDDEN", document.plain_text)
        self.assertNotIn("EVIL", document.plain_text)
        self.assertNotIn("EMBEDDED CONTENT", document.plain_text)
        self.assertIn("Evidence table", document.plain_text)
        self.assertEqual([b.level for b in document.blocks if b.kind == "heading"], [1])
        self.assertEqual([b.attrs["ordered"] for b in document.blocks if b.kind == "list_item"], [True, True, False])
        table = next(b for b in document.blocks if b.kind == "table")
        self.assertEqual(table.attrs["rows"], [["Name", "Value"], ["A", "1 < 2"]])
        self.assertIn("html-images-omitted", {w.code for w in document.warnings})
        self.assertFalse(document.metadata["network_access"])

    def test_eight_languages_and_html_charset_declaration(self):
        for value in LANGUAGES:
            with self.subTest(value=value):
                self.assertEqual(parse(".htm", "<p>" + value + "</p>").plain_text, value)
        for codec, value in (("cp1251", LANGUAGES[6]), ("shift_jis", LANGUAGES[5]), ("big5", LANGUAGES[2]), ("cp1252", LANGUAGES[4])):
            html = '<meta charset="' + codec + '"><p>' + value + '</p>'
            with self.subTest(codec=codec):
                document = parse(".html", html.encode(codec))
                self.assertEqual(document.plain_text, value)
                self.assertFalse(document.metadata["encoding_ambiguous"])
        # Meta-looking strings in scripts are not encoding declarations.
        document = parse(".html", '<script>"<meta charset=cp1251>"</script><p>Français</p>')
        self.assertEqual(document.plain_text, "Français")
        document = parse(".html", '<meta charset=cp1251><p>Москва</p>'.encode("cp1251"), encoding="auto")
        self.assertEqual(document.plain_text, "Москва")

    def test_html_spans_nested_tables_pre_and_omitted_end_tags(self):
        html = '<pre>one  two\n  three</pre><table><tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td></tr></table>'
        document = parse(".html", html)
        self.assertIn("one  two\n  three", document.plain_text)
        table = next(b for b in document.blocks if b.kind == "table")
        self.assertEqual(table.attrs["rows"], [["A", "B"], ["", "C"]])
        cell = next(b for b in document.blocks if b.text == "C")
        self.assertEqual((cell.location.row, cell.location.column), (1, 1))
        self.assertEqual(document.to_dict(), parse(".html", html).to_dict())
        nested = parse(".html", '<table><tr><td>Outer<table><tr><td>Inner</td></tr></table>Tail</td></tr></table>')
        self.assertIn("Inner", nested.plain_text)
        self.assertEqual(nested.quality.table_count, 1)
        self.assertIn("html-nested-table-linearized", {w.code for w in nested.warnings})
        unclosed = parse(".html", '<table><tr><td>A<td>B')
        self.assertEqual(unclosed.blocks[0].attrs["rows"], [["A", "B"]])
        self.assertIn("html-unclosed-table", {w.code for w in unclosed.warnings})

    def test_html_safety_bounds(self):
        for html in ('<div>' * 129 + 'too deep', '<table><tr><td colspan="999999999999">x</td></tr></table>',
                     '<table><tr><td rowspan="1000" colspan="1000">x</td></tr></table>'):
            with self.subTest(html=html[:60]), self.assertRaises(IngestionError):
                parse(".html", html)
        self.assertEqual(parse(".html", '<meta http-equiv><p>safe</p>').plain_text, "safe")

    def test_optional_end_tags_and_hidden_template_cannot_escape(self):
        html = '<title>Implicit header</title><body>' + ''.join('<p>Paragraph ' + str(i) for i in range(200))
        document = parse(".html", html)
        self.assertEqual(document.title, "Implicit header")
        self.assertEqual(len(document.blocks), 200)
        self.assertEqual(document.blocks[-1].text, "Paragraph 199")
        document = parse(".html", '<head><title>Title</title><body><ul><li>A<li>B</ul>')
        self.assertEqual([b.text for b in document.blocks], ["A", "B"])
        for html in ('<body><p>Visible</p><template><div>Secret</body>Secret tail</template>',
                     '<head><template><body>Secret</body></template></head><body>Visible</body>'):
            with self.subTest(html=html):
                self.assertNotIn("Secret", parse(".html", html).plain_text)


class RTFTextTests(unittest.TestCase):
    def test_unicode_preserves_eight_languages_and_surrogate_pairs(self):
        for value in (*LANGUAGES, "Supplementary: 😀 𠀀"):
            with self.subTest(value=value):
                raw = (r"{\rtf1\ansi\uc1 " + rtf_unicode(value) + "}").encode("ascii")
                document = parse(".rtf", raw)
                self.assertEqual(document.plain_text, value)
                self.assertEqual(document.to_dict(), parse(".rtf", raw).to_dict())

    def test_russian_japanese_chinese_and_western_codepages(self):
        for page, codec, text in ((1251, "cp1251", LANGUAGES[6]), (932, "cp932", LANGUAGES[5]),
                                  (936, "gbk", LANGUAGES[1]), (950, "big5", LANGUAGES[2]),
                                  (1252, "cp1252", LANGUAGES[3] + LANGUAGES[4])):
            with self.subTest(page=page):
                raw = b"{\\rtf1\\ansi\\ansicpg" + str(page).encode() + b" " + hex_run(text, codec) + b"}"
                self.assertEqual(parse(".rtf", raw).plain_text, text)
        # ソ uses a Shift-JIS trailing 0x5c byte which is not an RTF control.
        raw = b"{\\rtf1\\ansi\\ansicpg932 " + "ソフトウェア".encode("cp932") + b"}"
        self.assertEqual(parse(".rtf", raw).plain_text, "ソフトウェア")

    def test_font_charset_switches_and_default_font(self):
        raw = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fnil\\fcharset128 MS Gothic;}"
               b"{\\f1\\fnil\\fcharset204 Arial;}}" + hex_run("日本語", "cp932") + b"\\par\\f1 " + hex_run("Москва", "cp1251") + b"}")
        self.assertEqual(parse(".rtf", raw).plain_text, "日本語\n\nМосква")

    def test_unicode_fallback_group_scoping_and_unicode_alternative(self):
        raw = rb"{\rtf1\ansi\uc2 \u20013\'d6\'d0{\uc0\u25991} \u23383??\par{\upr{ANSI fallback}{\*\ud Unicode \u33391??}}}"
        self.assertEqual(parse(".rtf", raw).plain_text, "中文 字\n\nUnicode 良")
        raw = rb"{\rtf1 Escaped \{braces\} and \\ slash\par \emdash\tab text}"
        self.assertEqual(parse(".rtf", raw).plain_text, "Escaped {braces} and \\ slash\n\n—\ttext")

    def test_hidden_objects_binary_fields_and_tables_are_explicit(self):
        binary = b"{\\\x00}"
        raw = (rb"{\rtf1 Before {\pict\bin" + str(len(binary)).encode() + b" " + binary + rb"}{\object\objdata deadbeef}"
               rb"{\v HIDDEN}{\*\unknown CONTENT}{\field{\*\fldinst HYPERLINK \"https://invalid.example\"}{\fldrslt Link label}}"
               rb"\par\trowd A\cell B\cell\row After}")
        document = parse(".rtf", raw)
        self.assertNotIn("deadbeef", document.plain_text)
        self.assertNotIn("HIDDEN", document.plain_text)
        self.assertNotIn("CONTENT", document.plain_text)
        self.assertNotIn("https", document.plain_text)
        self.assertIn("Link label", document.plain_text)
        self.assertIn("A\tB", document.plain_text)
        codes = {w.code for w in document.warnings}
        self.assertTrue({"rtf-content-omitted", "rtf-table-linearized"} <= codes)
        self.assertIn("binary", document.metadata["omitted_destinations"])
        self.assertEqual(parse(".rtf", rb"{\rtf1 Visible \v HIDDEN\plain visible again}").plain_text,
                         "Visible visible again")

    def test_rtf_rejects_malformed_or_excessive_structures(self):
        examples = [b"not rtf", rb"{\rtf1 unfinished", rb"{\rtf1 text} extra", rb"{\rtf1 \'xz}",
                    rb"{\rtf1\bin9999 short}", rb"{\rtf1\uc999 text}", rb"{\rtf1\u999999?}",
                    rb"{\rtf1\u-10179?}", rb"{\rtf1\ansicpg99999 text}",
                    b"{\\rtf1 " + b"{" * 129 + b"x" + b"}" * 130]
        for raw in examples:
            with self.subTest(raw=raw[:50]), self.assertRaises(IngestionError):
                parse(".rtf", raw)
        with self.assertRaises(IngestionError):
            parse(".rtf", rb"{\rtf1 A}", encoding="unicode_escape")


if __name__ == "__main__":
    unittest.main()
