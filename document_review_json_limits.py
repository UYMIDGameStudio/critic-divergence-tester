"""Measure canonical UTF-8 JSON without allocating its escaped representation."""
import math


class JSONSizeLimitError(ValueError):
    pass


_SHORT_CONTROLS = '\b\t\n\f\r'
_LONG_CONTROLS = tuple(chr(n) for n in range(32) if chr(n) not in _SHORT_CONTROLS)


def json_encoded_size(value, maximum: int) -> int:
    """Exact size for JSON-ready data, compact separators and a trailing LF.

    Use the same encoding as canonical_json: ensure_ascii=False, UTF-8, and
    finite JSON numbers. Traversal is iterative and strings use bounded chunks.
    Shared strings are measured once, but charged for every serialized use.
    """
    if type(maximum) is not int or maximum < 0:
        raise ValueError('JSON byte limit must be a non-negative integer')
    total, strings, active = 0, {}, set()

    def charge(amount):
        nonlocal total
        if amount > maximum-total:
            raise JSONSizeLimitError('Expanded JSON exceeds its byte budget')
        total += amount

    def string_size(text):
        key = id(text)
        if key in strings:
            return strings[key]
        available = maximum-total
        if len(text)+2 > available:
            raise JSONSizeLimitError('Expanded JSON exceeds its byte budget')
        size = 2
        for start in range(0, len(text), 8192):
            piece = text[start:start+8192]
            size += len(piece.encode('utf-8')) + piece.count('"') + piece.count('\\')
            if not piece.isprintable():
                size += sum(piece.count(char) for char in _SHORT_CONTROLS)
                size += 5*sum(piece.count(char) for char in _LONG_CONTROLS)
            if size > available:
                raise JSONSizeLimitError('Expanded JSON exceeds its byte budget')
        strings[key] = size
        return size

    def dictionary_values(mapping):
        for key, child in mapping.items():
            if not isinstance(key, str):
                raise ValueError('JSON-ready objects require text keys')
            yield key
            yield child

    charge(1)  # canonical_json terminates the document with LF.
    stack = [(iter((value,)), None)]
    while stack:
        iterator, container = stack[-1]
        try:
            current = next(iterator)
        except StopIteration:
            stack.pop()
            if container is not None:
                active.remove(container)
            continue
        if isinstance(current, (dict, list, tuple)):
            identifier = id(current)
            if identifier in active:
                raise ValueError('Circular reference is not JSON')
            charge(2 + max(0, len(current)-1) + (len(current) if isinstance(current, dict) else 0))
            active.add(identifier)
            stack.append((iter(dictionary_values(current) if isinstance(current, dict) else current), identifier))
        elif isinstance(current, str):
            charge(string_size(current))
        elif current is None:
            charge(4)
        elif current is True:
            charge(4)
        elif current is False:
            charge(5)
        elif isinstance(current, int):
            charge(len(int.__repr__(current)))
        elif isinstance(current, float) and math.isfinite(current):
            charge(len(float.__repr__(current)))
        else:
            raise ValueError('Value is not finite JSON-ready data')
    return total
