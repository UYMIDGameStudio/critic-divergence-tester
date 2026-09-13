"""Strict document text decoding with explicit, inspectable encoding choices.

Byte-order marks and strong Unicode byte patterns take priority. Legacy
encodings can describe the same bytes differently: automatic selection keeps
those alternatives visible instead of claiming infallible language detection.
No decoding path replaces or ignores undecodable input.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
import re


class TextDecodingError(ValueError):
    """The bytes or requested encoding cannot be safely treated as text."""


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    ambiguous: bool = False
    candidates: tuple[str, ...] = ()


# Resolve aliases only through Python's codec registry, then require an exact
# allowlist match: transformation codecs (escape, compression, ROT13) are not
# document encodings and must never become executable decoding alternatives.
_ENCODING_NAMES = (
    "ascii", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
    "utf-32", "utf-32-le", "utf-32-be", "gb18030", "gbk", "gb2312",
    "big5", "big5hkscs", "cp950", "shift_jis", "cp932", "euc_jp",
    "iso2022_jp", "euc_kr", "cp949", "cp1250", "cp1251", "cp1252",
    "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258",
    "iso8859-1", "iso8859-2", "iso8859-5", "iso8859-7", "iso8859-9",
    "iso8859-15", "koi8-r", "koi8-u", "cp437", "cp850",
)
SUPPORTED_ENCODINGS = tuple(dict.fromkeys(codecs.lookup(name).name for name in _ENCODING_NAMES))
_ALLOWED_ENCODINGS = frozenset(SUPPORTED_ENCODINGS)
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_BINARY_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff]")


def normalize_encoding(encoding: str | None) -> str | None:
    """Return a whitelisted canonical codec name; ``auto`` means detection."""
    if encoding is None:
        return None
    if not isinstance(encoding, str):
        raise TextDecodingError("文本编码必须是名称或 auto")
    name = encoding.strip()
    if not name or name.casefold() == "auto":
        return None
    try:
        canonical = codecs.lookup(name).name
    except LookupError as exc:
        raise TextDecodingError("未知文本编码，请选择支持的编码名称") from exc
    if canonical not in _ALLOWED_ENCODINGS:
        raise TextDecodingError("此编码不在支持的文本编码白名单中")
    return canonical


def _checked_decode(data: bytes, encoding: str) -> str:
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise TextDecodingError(f"文件无法按 {encoding} 完整解码，请选择原文件编码") from exc
    forbidden = _BINARY_CONTROLS.search(text)
    if forbidden:
        point = ord(forbidden.group())
        raise TextDecodingError(f"文本含 NUL 或不合理控制字符 U+{point:04X}，可能编码不符或文件为二进制")
    return text


def _bom(data: bytes) -> tuple[bytes, str] | None:
    return next(((prefix, name) for prefix, name in _BOMS if data.startswith(prefix)), None)


def _unmarked_unicode_candidates(data: bytes) -> list[str]:
    """Only consider UTF-16/32 when NUL placement supplies byte evidence."""
    if b"\0" not in data:
        return []
    candidates = []
    if len(data) >= 4 and len(data) % 4 == 0:
        for name, high, next_high in (("utf-32-le", 3, 2), ("utf-32-be", 0, 1)):
            if all(byte == 0 for byte in data[high::4]) and all(byte <= 0x10 for byte in data[next_high::4]):
                candidates.append(name)
    if len(data) >= 4 and len(data) % 2 == 0:
        units = len(data) // 2
        for name, high, low in (("utf-16-le", 1, 0), ("utf-16-be", 0, 1)):
            zeros = data[high::2].count(0)
            other_zeros = data[low::2].count(0)
            if zeros >= 2 and zeros / units >= 0.30 and other_zeros / units <= 0.10:
                candidates.append(name)
    return candidates


def _decodable(data: bytes, encodings: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    candidates = []
    for name in encodings:
        try:
            text = _checked_decode(data, name)
        except TextDecodingError:
            continue
        candidates.append((name, text))
    return candidates


def _selected(candidates: list[tuple[str, str]]) -> DecodedText:
    # GB2312/GBK often produce exactly the GB18030 text. That is compatibility,
    # not a meaningful competing interpretation; retain one representative.
    distinct = []
    seen_texts = set()
    for name, text in candidates:
        if text not in seen_texts:
            distinct.append((name, text))
            seen_texts.add(text)
    name, text = distinct[0]
    ambiguous = len(distinct) > 1
    return DecodedText(text, name, ambiguous, tuple(name for name, _ in distinct) if ambiguous else ())


def _has_language_evidence(name: str, text: str) -> bool:
    """Limit extra legacy candidates; single-byte success alone is no signal."""
    letters = [char for char in text if char.isalpha()]
    if name == "shift_jis":
        # Half-width kana alone is a frequent false positive for Chinese bytes.
        return sum("\u3040" <= char <= "\u30ff" for char in text) >= 2
    if name == "cp1251":
        cyrillic = sum("\u0400" <= char <= "\u052f" for char in letters)
        return cyrillic >= 4 and cyrillic >= 0.6 * len(letters) and len(re.findall(r"[\u0400-\u052f]{2,}", text)) >= 2
    if name == "cp1252":
        western = sum("\u00c0" <= char <= "\u024f" for char in letters)
        ascii_letters = sum(char.isascii() for char in letters)
        return western >= 1 and ascii_letters >= 3 and ascii_letters + western == len(letters)
    return False


def decode_document_text(data: bytes, encoding: str | None = None) -> DecodedText:
    """Decode without data loss, exposing ambiguous legacy interpretations.

    An explicit encoding wins over automatic language guesses. UTF-16/32 with
    unspecified byte order needs a BOM or strong byte evidence; callers can
    always select the LE/BE codecs explicitly. Automatic legacy selection is
    provisional whenever different successful interpretations are returned.
    """
    if not isinstance(data, bytes):
        raise TextDecodingError("文本解码输入必须是原始文件字节")
    requested = normalize_encoding(encoding)
    marked = _bom(data)
    if requested is not None:
        if requested in {"utf-16", "utf-32"} and data:
            if marked and marked[1].startswith(requested + "-"):
                prefix, actual = marked
                return DecodedText(_checked_decode(data[len(prefix):], actual), actual)
            possible = _decodable(data, [name for name in _unmarked_unicode_candidates(data) if name.startswith(requested + "-")])
            if len(possible) == 1:
                actual, text = possible[0]
                return DecodedText(text, actual)
            raise TextDecodingError(f"{requested} 缺少明确字节序，请选择 {requested}-le 或 {requested}-be")
        if marked and (requested == marked[1] or requested == "utf-8" and marked[1] == "utf-8-sig"):
            data = data[len(marked[0]):]
        return DecodedText(_checked_decode(data, requested), requested)
    if marked:
        prefix, actual = marked
        return DecodedText(_checked_decode(data[len(prefix):], actual), actual)
    unicode_candidates = _decodable(data, _unmarked_unicode_candidates(data))
    if unicode_candidates:
        return _selected(unicode_candidates)
    try:
        return DecodedText(_checked_decode(data, "utf-8"), "utf-8")
    except TextDecodingError:
        pass
    candidates = _decodable(data, ("gb18030", "gbk", "gb2312", "big5"))
    for name, text in _decodable(data, ("shift_jis", "cp1251", "cp1252")):
        if _has_language_evidence(name, text):
            candidates.append((name, text))
    if candidates:
        return _selected(candidates)
    raise TextDecodingError("无法可靠识别文本编码或文件含二进制控制字符；请手动选择原文件的 UTF、GB18030、Big5、西欧、日文或俄文编码")


__all__ = ["DecodedText", "TextDecodingError", "SUPPORTED_ENCODINGS", "normalize_encoding", "decode_document_text"]
