"""Eight-language, ambiguity and binary-rejection document decoding checks."""
from __future__ import annotations

import codecs
import unittest

from document_text_encoding import (
    DecodedText,
    SUPPORTED_ENCODINGS,
    TextDecodingError,
    decode_document_text,
    normalize_encoding,
)


LANGUAGES = {
    "English": "The review preserves every claim and its evidence.",
    "Simplified Chinese": "这是一段简体中文，保留证据与修改记录。",
    "Traditional Chinese": "這是一段繁體中文，保留證據與修改紀錄。",
    "German": "Grüße aus Köln: Größe und äußere Prüfung.",
    "French": "L’étude française préserve les caractères œ et é.",
    "Japanese": "日本語の文章です。漢字とひらがな、カタカナを保持します。",
    "Russian": "Русский текст: доказательства, ёлка и исправления.",
    "Latin": "Lingua Latīna: māter, æquus, cœlum.",
}


class DocumentTextEncodingTests(unittest.TestCase):
    def test_eight_languages_are_lossless_in_utf8_and_bom_unicode(self):
        for language, text in LANGUAGES.items():
            for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
                with self.subTest(language=language, encoding=encoding):
                    decoded = decode_document_text(text.encode(encoding))
                    self.assertEqual(decoded.text, text)
                    self.assertFalse(decoded.ambiguous)

    def test_mixed_language_document_preserves_all_scripts_and_astral_characters(self):
        text = "\n\n".join(LANGUAGES.values()) + "\nSupplementary character: 𠀀; symbol: 😀.\n"
        encodings = (("utf-8", b""), ("utf-8", codecs.BOM_UTF8),
                     ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
                     ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE))
        for encoding, bom in encodings:
            with self.subTest(encoding=encoding, bom=bom):
                decoded = decode_document_text(bom + text.encode(encoding))
                self.assertEqual(decoded.text, text)
                self.assertFalse(decoded.ambiguous)

    def test_strong_byte_evidence_detects_unmarked_utf16_and_utf32(self):
        text = "English heading\n" + "\n".join(LANGUAGES.values()) + "\n"
        for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
            with self.subTest(encoding=encoding):
                decoded = decode_document_text(text.encode(encoding))
                self.assertEqual(decoded.text, text)
                self.assertEqual(decoded.encoding, encoding)
                self.assertFalse(decoded.ambiguous)

    def test_utf16_without_ascii_byte_evidence_can_be_selected_explicitly(self):
        text = "你好世界"
        for encoding in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                decoded = decode_document_text(text.encode(encoding), encoding)
                self.assertEqual(decoded.text, text)

    def test_explicit_legacy_encodings_cover_requested_languages(self):
        cases = (
            ("English", "ascii", LANGUAGES["English"]),
            ("Simplified Chinese", "gb2312", LANGUAGES["Simplified Chinese"]),
            ("Simplified Chinese", "gbk", LANGUAGES["Simplified Chinese"]),
            ("Simplified Chinese", "gb18030", LANGUAGES["Simplified Chinese"]),
            ("Traditional Chinese", "big5", LANGUAGES["Traditional Chinese"]),
            ("Traditional Chinese", "big5hkscs", LANGUAGES["Traditional Chinese"]),
            ("German", "cp1252", LANGUAGES["German"]),
            ("German", "iso8859-1", LANGUAGES["German"]),
            ("French", "cp1252", LANGUAGES["French"]),
            ("French", "iso8859-1", "Français : élève, Noël, café."),
            ("French", "iso8859-15", "L'étude française préserve les caractères œ et é."),
            ("Japanese", "shift_jis", LANGUAGES["Japanese"]),
            ("Japanese", "cp932", LANGUAGES["Japanese"]),
            ("Japanese", "euc_jp", LANGUAGES["Japanese"]),
            ("Japanese", "iso2022_jp", LANGUAGES["Japanese"]),
            ("Russian", "cp1251", LANGUAGES["Russian"]),
            ("Russian", "koi8-r", LANGUAGES["Russian"]),
            ("Russian", "iso8859-5", LANGUAGES["Russian"]),
            ("Latin", "iso8859-15", "Lingua Latina: æquus, cœlum."),
        )
        for language, encoding, text in cases:
            with self.subTest(language=language, encoding=encoding):
                decoded = decode_document_text(text.encode(encoding), encoding)
                self.assertEqual(decoded.text, text)
                self.assertEqual(decoded.encoding, codecs.lookup(encoding).name)
                self.assertFalse(decoded.ambiguous)

    def test_gb_family_auto_decode_does_not_require_utf8(self):
        text = LANGUAGES["Simplified Chinese"]
        for encoding in ("gb2312", "gbk", "gb18030"):
            with self.subTest(encoding=encoding):
                decoded = decode_document_text(text.encode(encoding))
                self.assertEqual(decoded.text, text)
                self.assertEqual(decoded.encoding, "gb18030")
        supplementary = "扩展汉字𠀀和表情😀"
        self.assertEqual(decode_document_text(supplementary.encode("gb18030")).text, supplementary)

    def test_gb18030_big5_ambiguity_is_visible_and_explicit_choice_is_honored(self):
        raw = "中文".encode("big5")
        decoded = decode_document_text(raw)
        self.assertTrue(decoded.ambiguous)
        self.assertIn("gb18030", decoded.candidates)
        self.assertIn("big5", decoded.candidates)
        self.assertNotEqual(decoded.text, "中文")
        chosen = decode_document_text(raw, "Big5")
        self.assertEqual(chosen.text, "中文")
        self.assertFalse(chosen.ambiguous)
        self.assertEqual(chosen.candidates, ())

    def test_auto_candidates_include_japanese_and_russian_when_text_gives_evidence(self):
        for name, source in (("shift_jis", LANGUAGES["Japanese"]), ("cp1251", LANGUAGES["Russian"])):
            with self.subTest(encoding=name):
                decoded = decode_document_text(source.encode(name))
                self.assertIn(name, (decoded.encoding, *decoded.candidates))
                if decoded.text != source:
                    self.assertTrue(decoded.ambiguous)
                self.assertEqual(decode_document_text(source.encode(name), name).text, source)

    def test_aliases_are_normalized_without_decoding_probe_bytes(self):
        for alias, expected in (("UTF8", "utf-8"), ("UTF_16_LE", "utf-16-le"),
                                ("Windows-1252", "cp1252"), ("Windows-1251", "cp1251"),
                                ("latin-1", "iso8859-1"), ("ISO-8859-15", "iso8859-15"),
                                ("SJIS", "shift_jis"), ("EUC-JP", "euc_jp"),
                                ("ISO-2022-JP", "iso2022_jp"), ("KOI8-R", "koi8-r")):
            with self.subTest(alias=alias):
                self.assertEqual(normalize_encoding(alias), expected)
                self.assertIn(expected, SUPPORTED_ENCODINGS)
        for automatic in (None, "", "  ", "AUTO", " auto "):
            self.assertIsNone(normalize_encoding(automatic))

    def test_unknown_or_transformation_codecs_are_not_document_encodings(self):
        for encoding in ("does-not-exist", "rot13", "base64_codec", "unicode_escape", "raw_unicode_escape", "utf-7", 42):
            with self.subTest(encoding=encoding), self.assertRaises(TextDecodingError):
                normalize_encoding(encoding)

    def test_explicit_selection_never_silently_replaces_bad_bytes(self):
        for data, encoding in ((b"\xff", "utf-8"), (b"\x81", "cp1252"),
                               (b"\x81\x30\x81", "gb18030"), (b"\x82", "shift_jis")):
            with self.subTest(data=data, encoding=encoding), self.assertRaises(TextDecodingError):
                decode_document_text(data, encoding)

    def test_bom_detection_checks_utf32_before_utf16_and_fails_on_truncation(self):
        for encoding, bom in (("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE)):
            decoded = decode_document_text(bom + "AB".encode(encoding))
            self.assertEqual(decoded.text, "AB")
            self.assertEqual(decoded.encoding, encoding)
        for data in (codecs.BOM_UTF8 + b"\xc3", codecs.BOM_UTF16_LE + b"A", codecs.BOM_UTF32_BE + b"\0\0A"):
            with self.subTest(data=data), self.assertRaises(TextDecodingError):
                decode_document_text(data)

    def test_binary_controls_are_rejected_after_decoding(self):
        for encoding in ("utf-8", "utf-16-le", "utf-32-be", "gb18030", "cp1252"):
            for control in ("\0", "\x01", "\x1b", "\x7f"):
                with self.subTest(encoding=encoding, control=control), self.assertRaises(TextDecodingError):
                    decode_document_text(("Readable" + control + "text").encode(encoding), encoding)
        for data in (b"\0\0\0\0", b"\x89PNG\r\n\x1a\n", b"MZ\0abc\0data", b"\x01\x02binary\x03"):
            with self.subTest(data=data), self.assertRaises(TextDecodingError):
                decode_document_text(data)

    def test_valid_text_whitespace_is_preserved(self):
        text = "Header\r\nColumn\tValue\n\fNext page\n"
        self.assertEqual(decode_document_text(text.encode()).text, text)
        self.assertEqual(decode_document_text(b""), DecodedText("", "utf-8"))

    def test_ambiguous_unmarked_utf32_byte_order_is_not_claimed_as_certain(self):
        raw = b"\x00\x01\x00\x00"
        decoded = decode_document_text(raw)
        self.assertTrue(decoded.ambiguous)
        self.assertEqual(set(decoded.candidates), {"utf-32-le", "utf-32-be"})
        with self.assertRaises(TextDecodingError):
            decode_document_text(raw, "utf-32")
        self.assertEqual(decode_document_text(raw, "utf-32-be").text, "\U00010000")


if __name__ == "__main__":
    unittest.main()
