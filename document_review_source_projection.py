"""Lossless historical block references for source-bound recheck prompts.

All policy text outside the historical document JSON is retained verbatim.
References are allowed only for JSON-identical blocks in the current full IR.
"""
from __future__ import annotations

import hashlib
import json
import math

from document_review_model import canonical_json


HISTORY_MARKER = '## Bound original review snapshot\n'
SOURCE_MARKER = '## Internal document blocks\n```json\n'
REVISED_MARKER = '## Revised document blocks\n```json\n'


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError('Duplicate document JSON field')
        value[key] = item
    return value


def _constant(value):
    raise ValueError('Non-finite document JSON number')


def _float(value):
    result = float(value)
    if not math.isfinite(result) or json.dumps(result) != value:
        raise ValueError('Document JSON number cannot be preserved exactly')
    return result


def _int(value):
    result = int(value)
    if str(result) != value:
        raise ValueError('Document JSON number cannot be preserved exactly')
    return result


def _section(text, marker, *, last=False):
    start = (text.rindex(marker) if last else text.index(marker)) + len(marker)
    value, length = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float, parse_int=_int).raw_decode(text[start:])
    end = start + length
    if not text.startswith('\n```', end):
        raise ValueError('Unrecognized document section closing delimiter')
    return value, start, end


def _blocks_by_id(blocks):
    if not isinstance(blocks, list):
        raise ValueError('Document blocks must be an array')
    result = {}
    for block in blocks:
        if (not isinstance(block, dict) or not {'block_id', 'kind', 'text'} <= set(block)
                or not isinstance(block['block_id'], str) or not block['block_id']
                or not isinstance(block['kind'], str) or not isinstance(block['text'], str)
                or block['block_id'] in result):
            raise ValueError('Ambiguous or malformed document blocks')
        result[block['block_id']] = block
    return result


def project_historical_prompt(original: bytes, current_blocks: list[dict]) -> tuple[str, dict]:
    """Keep every original block and instruction, referencing identical blocks."""
    text = original.decode('utf-8')
    current = _blocks_by_id(current_blocks)
    metadata = {'version': 1, 'mode': 'verbatim', 'reason': 'unrecognized-document-section',
        'original_prompt_file_sha256': hashlib.sha256(original).hexdigest(),
        'original_prompt_bytes': len(original), 'projected_prompt_bytes': len(original),
        'projected_prompt_sha256': hashlib.sha256(original).hexdigest(),
        'saved_bytes': 0, 'historical_block_count': None, 'reused_block_count': 0,
        'literal_block_count': None, 'historical_blocks_sha256': None,
        'revised_blocks_sha256': hashlib.sha256(canonical_json(current_blocks)).hexdigest()}
    try:
        if text.count(SOURCE_MARKER) != 1:
            return text, metadata
        blocks, start, end = _section(text, SOURCE_MARKER)
        _blocks_by_id(blocks)
        historical_digest = hashlib.sha256(canonical_json(blocks)).hexdigest()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return text, metadata
    metadata.update(historical_block_count=len(blocks), literal_block_count=len(blocks),
                    historical_blocks_sha256=historical_digest)
    entries, reused = [], 0
    for block in blocks:
        counterpart = current.get(block['block_id'])
        # JSON identity preserves distinctions such as true/1 and 1/1.0.
        if counterpart is not None and canonical_json(block) == canonical_json(counterpart):
            entries.append({'same_as_current': block['block_id']})
            reused += 1
        else:
            entries.append({'historical_block': block})
    if not reused:
        metadata['reason'] = 'no-identical-blocks'
        return text, metadata
    payload = {'representation': 'lossless-history-v1',
        'rule': 'Keep this sequence and order. Replace each same_as_current reference with the JSON-identical block in Revised document blocks below. historical_block is the complete older block, including deleted blocks; it is not the current quotation source. No historical block has been summarized or discarded.',
        'entries': entries}
    projected = text[:start] + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + text[end:]
    raw = projected.encode('utf-8')
    if len(raw) >= len(original):
        metadata['reason'] = 'not-beneficial'
        return text, metadata
    metadata.update(mode='lossless-current-block-references', reason='identical-blocks-reused',
        projected_prompt_bytes=len(raw), projected_prompt_sha256=hashlib.sha256(raw).hexdigest(),
        saved_bytes=len(original) - len(raw), reused_block_count=reused,
        literal_block_count=len(blocks) - reused)
    return projected, metadata


def validate_historical_projection(request, prompt: bytes, original: bytes, current_blocks: list[dict]) -> list[str]:
    """Reject missing, stale, lossy or forged projections; old prompts stay valid."""
    marker = HISTORY_MARKER.encode('utf-8')
    version = request.get('schema_version', 1)
    if type(version) is not int or version not in {1, 2, 3, 4}:
        return ['Unsupported external recheck request version']
    required = type(version) is int and version >= 4
    if not required and 'historical_prompt_projection' not in request and marker not in prompt:
        return []
    metadata = request.get('historical_prompt_projection')
    if not isinstance(metadata, dict) or type(metadata.get('version')) is not int or metadata['version'] != 1:
        return ['Unsupported or missing historical source projection']
    try:
        expected, expected_metadata = project_historical_prompt(original, current_blocks)
        if canonical_json(metadata) != canonical_json(expected_metadata):
            return ['Historical source projection metadata does not match its bound sources']
        start = prompt.index(marker) + len(marker)
        expected_bytes = expected.encode('utf-8')
        if (prompt[start:start + len(expected_bytes)] != expected_bytes
                or not prompt[start + len(expected_bytes):].startswith(b'\n\n## Original Findings\n')):
            return ['Historical source projection differs from the original review policy or blocks']
        current, _, _ = _section(prompt.decode('utf-8'), REVISED_MARKER, last=True)
        if canonical_json(current) != canonical_json(current_blocks):
            return ['Recheck prompt does not contain the complete bound revised document']
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeDecodeError):
        return ['Historical source projection or revised document section is malformed']
    return []
