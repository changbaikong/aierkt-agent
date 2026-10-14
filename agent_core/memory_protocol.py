"""Shared model output contract and mechanical source/legacy normalization.

Offsets count Unicode characters in the original message, with an exclusive end.
This module checks shape and references. It never infers adoption or lifetime.
"""
from __future__ import annotations

from copy import deepcopy
import json

from jsonschema import Draft202012Validator

PROTOCOL_VERSION = 'memory-items-1'
#: 代理提示版本。1.5：新增 clarification_context 的"只是可能关联、不得自动确认"说明。
#: 提示一改就 +1 —— 否则旧缓存会冒充新判断（见 memory_model_agent 的缓存键）。
PROMPT_VERSION = 'memory-items-1.5'

QUOTE_SCHEMA = {
    'type': 'object', 'required': ['source_id', 'start', 'end'],
    'properties': {'source_id': {'type': 'string', 'minLength': 1},
                   'start': {'type': 'integer', 'minimum': 0},
                   'end': {'type': 'integer', 'minimum': 1}},
    'additionalProperties': False,
}
LIFETIME_SCHEMA = {
    'type': 'object', 'required': ['mode', 'basis'],
    'properties': {'mode': {'enum': ['long_term', 'short_term', 'phase', 'uncertain']},
                   'days': {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 365000},
                   'basis': {'type': 'string', 'minLength': 1},
                   'policy': {'enum': ['explicit_duration', 'one_request', 'daily_emotion', 'reminder', 'phase', 'durable', 'uncertain']},
                   'end_condition': {'type': ['string', 'null']}},
    'allOf': [{'if': {'properties': {'mode': {'const': 'short_term'}}},
               'then': {'required': ['days'], 'properties': {'days': {'type': 'integer'}}}}],
    'additionalProperties': False,
}
ITEM_SCHEMA = {
    'type': 'object',
    'properties': {
        'index': {'type': 'integer', 'minimum': 0},
        'verdict': {'enum': ['supported', 'unsupported', 'uncertain']},
        'content': {'type': 'string', 'minLength': 1},
        'subject': {'type': 'string', 'minLength': 1},
        'reason': {'type': 'string', 'minLength': 1},
        'quotes': {'type': 'array', 'minItems': 1, 'items': QUOTE_SCHEMA},
        'same_subject': {'type': 'boolean'},
        'preserves_negation': {'type': 'boolean'},
        'preserves_conditions': {'type': 'boolean'},
        'change_authorized': {'type': 'boolean'},
        'adoption': {'type': 'boolean'},
        'adoption_binding': {'type': 'object', 'required': ['proposal', 'acknowledgement'],
                             'properties': {'proposal': QUOTE_SCHEMA, 'acknowledgement': QUOTE_SCHEMA},
                             'additionalProperties': False},
        'content_status': {'type': 'string'},
        'kind': {'type': 'string'},
        'scope_kind': {'enum': ['conversation', 'project', 'owner']},
        'write_action': {'enum': ['add', 'keep_existing', 'correct', 'replace', 'uncertain', 'reject']},
        'certainty': {'enum': ['certain', 'uncertain']},
        'doubt': {'type': ['string', 'null']},
        'clarification_question': {'type': ['string', 'null']},
        'target_id': {'type': ['string', 'null']},
        'target_version': {'type': ['integer', 'null'], 'minimum': 1},
        'same_fact': {'type': 'boolean'},
        'creates_new_fact': {'type': 'boolean'},
        'existing_memory_id': {'type': 'string'},
        'supersedes': {'type': 'array', 'maxItems': 1, 'uniqueItems': True,
                      'items': {'type': 'string', 'minLength': 1}},
        'lifetime': LIFETIME_SCHEMA,
    },
    'required': ['index', 'verdict'],
    'allOf': [{'if': {'properties': {'verdict': {'const': 'supported'}}},
               'then': {'required': ['content', 'subject', 'reason', 'quotes', 'same_subject',
                                    'preserves_negation', 'preserves_conditions', 'change_authorized', 'lifetime']}}],
    'additionalProperties': False,
}
RESPONSE_SCHEMA = {
    'type': 'object', 'required': ['items'],
    'properties': {'protocol_version': {'const': PROTOCOL_VERSION},
                   'items': {'type': 'array', 'items': ITEM_SCHEMA}},
    'additionalProperties': False,
}


def response_schema(*, agent=False):
    """Generate the agent profile from the same field and reference definition."""
    schema = deepcopy(RESPONSE_SCHEMA)
    if agent:
        item = schema['properties']['items']['items']
        item['required'] += ['write_action', 'reason']
        item['allOf'].append({
            'if': {'properties': {'verdict': {'enum': ['supported', 'uncertain']}}},
            'then': {'required': ['certainty', 'kind', 'content_status', 'scope_kind',
                'content', 'subject', 'quotes', 'lifetime', 'same_subject',
                'preserves_negation', 'preserves_conditions', 'change_authorized']}})
        item['allOf'].append({
            'if': {'properties': {'certainty': {'const': 'uncertain'}}, 'required': ['certainty']},
            'then': {'required': ['doubt', 'clarification_question'], 'properties': {
                'doubt': {'type': 'string', 'minLength': 1},
                'clarification_question': {'type': 'string', 'minLength': 1}}}})
    return schema


def source_catalog(packet):
    """Whole original messages, rather than tokenizer-dependent clauses."""
    if packet is None:
        return []
    rows, seen = [], {}

    def add(message_id, text, source_type):
        if not text:
            return
        identity = (message_id, str(text), source_type)
        if identity in seen:
            return
        source_id = f's{len(rows)}'
        seen[identity] = source_id
        rows.append(dict(source_id=source_id, message_id=message_id, text=str(text), source_type=source_type))

    add(packet.turn_id, packet.user_text, 'user_statement')
    for history in packet.relevant_history:
        if history.role in ('user', 'assistant'):
            add(history.message_id, history.content, 'user_statement' if history.role == 'user' else 'assistant_proposal')
    for ref in packet.evidence_refs:
        if ref.outcome in ('success', 'succeeded', 'completed'):
            add(ref.ref_id, ref.summary, 'tool_result')
    return rows


def _span(quote, sources):
    if not isinstance(quote, dict):
        raise ValueError('invalid_quote')
    if 'source_id' in quote:
        return {key: quote.get(key) for key in ('source_id', 'start', 'end')}
    # Legacy conversion is exact substring lookup, never paraphrase matching.
    text = quote.get('quote')
    if not isinstance(text, str) or not text:
        raise ValueError('invalid_quote')
    matches = []
    for source in sources:
        if source['message_id'] != quote.get('message_id'):
            continue
        start = source['text'].find(text)
        if start >= 0:
            matches.append(dict(source_id=source['source_id'], start=start, end=start + len(text)))
    if len(matches) != 1:
        raise ValueError('unknown_or_ambiguous_quote')
    return matches[0]


def resolve_quotes(quotes, sources):
    lookup = {s['source_id']: s for s in sources}
    rows = []
    if not isinstance(quotes, list) or not quotes:
        raise ValueError('invalid_quotes')
    for quote in quotes:
        span = _span(quote, sources)
        source = lookup.get(span.get('source_id'))
        start, end = span.get('start'), span.get('end')
        if source is None:
            raise ValueError('unknown_source_id')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source['text']):
            raise ValueError('invalid_source_span')
        rows.append(dict(**span, message_id=source['message_id'], source_type=source['source_type'],
                         quote=source['text'][start:end]))
    return rows


def _normalize_item(row, sources, *, parent=None):
    if not isinstance(row, dict):
        raise ValueError('invalid_item')
    result = deepcopy(row)
    if parent is not None:
        # A supported container explicitly applies to its declared parts. Never
        # inherit substantive flags, status, lifetime, or evidence from siblings.
        for key in ('index', 'verdict'):
            if key not in result and key in parent:
                result[key] = parent[key]
    quotes = result.get('quotes')
    if isinstance(quotes, list):
        nested = [q['adoption'] for q in quotes if isinstance(q, dict) and 'adoption' in q]
        if nested:
            if any(type(v) is not bool for v in nested) or len(set(nested)) != 1:
                raise ValueError('adoption_conflict')
            if 'adoption' in result and result['adoption'] != nested[0]:
                raise ValueError('adoption_conflict')
            result.setdefault('adoption', nested[0])
        result['quotes'] = [_span(q, sources) for q in quotes]
        resolved = resolve_quotes(result['quotes'], sources) if quotes else []
        if result.get('adoption') is True and 'adoption_binding' not in result:
            proposals = [q for q in resolved if q['source_type'] == 'assistant_proposal']
            acknowledgements = [q for q in resolved if q['source_type'] == 'user_statement']
            # Mechanical migration only when both roles were already cited.
            if len(proposals) == 1 and acknowledgements:
                result['adoption_binding'] = {
                    'proposal': {k: proposals[0][k] for k in ('source_id', 'start', 'end')},
                    'acknowledgement': {k: acknowledgements[-1][k] for k in ('source_id', 'start', 'end')},
                }
    binding = result.get('adoption_binding')
    if isinstance(binding, dict):
        for field in ('proposal', 'acknowledgement'):
            binding[field] = _span(binding.get(field), sources)
        resolve_quotes([binding['proposal'], binding['acknowledgement']], sources)
    return result


def decode_response(raw, *, packet=None, key='', expected_indices=None, sources=None, agent=False):
    """Return valid siblings and detailed formatting errors; preserve raw text."""
    result = dict(key=key, completed=True, raw=raw or '', items=[], format_errors=[],
                  protocol_version=PROTOCOL_VERSION, prompt_version=PROMPT_VERSION, blocked_indices=[])
    sources = source_catalog(packet) if sources is None else sources
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('items must be a list')
        if data.get('protocol_version', PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ValueError('protocol_version_mismatch')
    except (ValueError, TypeError):
        result.update(completed=False, error='invalid_review_response', needs_repair=True, repair_attempted=False)
        result['format_errors'].append(dict(index=None, path='$', code='invalid_review_response'))
        return result
    seen = set()
    validator = Draft202012Validator(response_schema(agent=agent)['properties']['items']['items'])
    for position, container in enumerate(data['items']):
        index = container.get('index') if isinstance(container, dict) else None
        if type(index) is not int or index < 0 or index in seen:
            result['format_errors'].append(dict(index=index, path=f'items[{position}].index', code='invalid_or_duplicate_index'))
            if type(index) is int and index >= 0 and index in seen:
                result['items'] = [r for r in result['items'] if r['index'] != index]
                result['blocked_indices'].append(index)
            continue
        seen.add(index)
        if expected_indices is not None and index not in expected_indices:
            result['format_errors'].append(dict(index=index, path=f'items[{position}].index', code='unexpected_index'))
            continue
        parts = container.get('parts')
        children = parts if isinstance(parts, list) and parts else [container]
        ordered_parts, valid_parts = [], []
        for part_index, child in enumerate(children):
            path = f'items[{position}]' + (f'.parts[{part_index}]' if children is parts else '')
            try:
                normalized = _normalize_item(child, sources, parent=container if children is parts else None)
                errors = sorted(validator.iter_errors(normalized), key=lambda e: str(e.path))
                if errors:
                    for err in errors:
                        result['format_errors'].append(dict(index=index, part_index=part_index if children is parts else None,
                            path=path + '.' + '.'.join(map(str, err.path)), code='invalid_field', detail=err.message))
                    ordered_parts.append({**normalized, 'protocol_error': 'invalid_field'})
                else:
                    valid_parts.append(normalized)
                    ordered_parts.append(normalized)
            except ValueError as exc:
                result['format_errors'].append(dict(index=index, path=path, code=str(exc)))
                ordered_parts.append({**(child if isinstance(child, dict) else {}), 'index': index,
                                      'protocol_error': str(exc)})
        if children is parts:
            result['items'].append(dict(index=index, verdict=container.get('verdict'), parts=ordered_parts))
        else:
            # Retain invalid slots too: their original negative judgments and
            # reference failures constrain the one permitted format repair.
            result['items'].extend(ordered_parts)
    if expected_indices is not None:
        for index in sorted(set(expected_indices) - seen):
            result['format_errors'].append(dict(index=index, path='items', code='missing_index'))
    # A nonexistent source or conflicting acknowledgement is a rejected
    # reference, not permission to invent another semantic answer.
    repairable = {'invalid_field', 'invalid_or_duplicate_index', 'unexpected_index', 'missing_index'}
    result['needs_repair'] = any(e['code'] in repairable for e in result['format_errors'])
    result['repair_attempted'] = False
    return result


def preserve_valid_items(initial, repaired):
    """A format repair can only replace failed slots, never valid judgments."""
    def replaceable(part):
        return (part.get('protocol_error') == 'invalid_field'
                and part.get('verdict') not in ('unsupported', 'uncertain')
                and not any(part.get(flag) is False for flag in
                            ('same_subject', 'preserves_negation', 'preserves_conditions', 'change_authorized', 'adoption')))

    outputs = {r['index']: r for r in repaired.get('items', [])}
    for row in initial.get('items', []):
        index = row['index']
        if 'parts' not in row:
            if not replaceable(row):
                outputs[index] = row
            else:
                outputs.setdefault(index, row)
            continue
        replacement = outputs.get(index, {})
        new_parts = replacement.get('parts') or []
        parts = [part if not replaceable(part) else
                 (new_parts[i] if i < len(new_parts) else part)
                 for i, part in enumerate(row['parts'])]
        outputs[index] = {**row, 'parts': parts}
    blocked = set(initial.get('blocked_indices', [])) | set(repaired.get('blocked_indices', []))
    return [outputs[i] for i in sorted(outputs) if i not in blocked]


def protocol_prompt():
    """The JSON contract printed to the model is the validator's same definition."""
    return ('统一协议版本 ' + PROTOCOL_VERSION + '。输出遵循以下结构定义；依据引用用 sources 中 source_id、start、end，'
            'start 从零计数，end 不包含在片段内，按原文字符计数。程序还原原文，不抄写句子。'
            'adoption 必须是与 index、verdict 同级的字段；认同须绑定 adoption_binding 的 proposal 和 acknowledgement。'
            '每个拆分事项独立返回全部字段。short_term 必须给 days；明确天数照原话，'
            '一次请求或当天情绪按政策一天、临时提醒三天，由你判断适用事项并写 policy 与理由。'
            '阶段决定使用 phase；“暂时”本身不等于一天；缺少期限判断不得默认补一天。\n'
            + json.dumps(RESPONSE_SCHEMA, ensure_ascii=False, separators=(',', ':')))
