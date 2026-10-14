"""Shared, source-addressed subject binding for every memory write path."""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from dataclasses import replace

from .memory_turn import clauses, content_units, expresses_adoption, value_tokens, value_evidenced


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def subject_text(text: str) -> str:
    raw = str(text or '')
    for token in value_tokens(raw):
        raw = raw.replace(token, '')
    raw = re.sub(r'用户|助手|要求|决定|选择|建议|方案|已经|尚未|继续|暂不|暂时|改用|改为|采用|使用|同意|以后|目前|为|是|的|了|元|人', '', raw)
    return re.sub(r'[^\w\u3400-\u9fff]', '', raw).strip()


def same_subject(left: str, right: str) -> bool:
    a, b = subject_text(left), subject_text(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if re.fullmatch(r'[A-Za-z]', a) and b.endswith(a):
        return True
    if re.fullmatch(r'[A-Za-z]', b) and a.endswith(b):
        return True
    return bool(content_units(a) & content_units(b)) or (len(a) >= 2 and a in b) or (len(b) >= 2 and b in a)


def unique_adopted_proposal(packet) -> tuple[str, str] | None:
    """A short explicit acknowledgement may inherit exactly one preceding proposal."""
    reply = str(packet.user_text or '').strip(' 。！!，,')
    if len(reply) > 18 or not expresses_adoption(reply):
        return None
    if not re.fullmatch(r'(嗯[，,\s]*)?(就这样|就这么办|就按这个[做办]?|按你说的[办做]?|同意|没问题)', reply):
        return None
    for history in reversed(packet.relevant_history):
        if history.role != 'assistant':
            continue
        text = str(history.content or '')
        if not any(word in text for word in ('建议', '可以吗', '要不要', '好吗', '是否')):
            return None
        if len(clauses(text)) != 1 or re.search(r'或者|或是|以及|另外|[AB][、.]|[12][、.]', text):
            return None
        return history.message_id, text
    return None


def bind_evidence(packet, content: str, *, source_refs=(), item_text='') -> list[dict]:
    if packet is None:
        return []
    wanted = set(source_refs or ())
    sources = []
    if not wanted or packet.turn_id in wanted:
        sources.append((packet.turn_id, str(packet.user_text or ''), 'user_statement'))
    sources.extend((h.message_id, h.content, 'user_statement') for h in packet.relevant_history
                   if h.role == 'user' and h.message_id in wanted)
    adopted = unique_adopted_proposal(packet)
    if adopted and same_subject(adopted[1], content):
        sources.append((adopted[0], adopted[1], 'adopted_proposal'))
    result = []
    for message_id, text, role in sources:
        parts = clauses(text)
        chosen = set()
        for claim in clauses(content):
            scored = []
            for index, quote in enumerate(parts):
                if same_subject(quote, claim):
                    score = SequenceMatcher(None, subject_text(claim), subject_text(quote)).ratio()
                    scored.append((score, index))
            # Select against each claim independently: common words cannot borrow
            # another subject's number; a second real status is not discarded.
            best = max((score for score, _ in scored), default=0)
            chosen.update(index for score, index in scored if score >= best - .05)
        # A bare correction inherits the supplied target, not an unrelated named
        # field. The denied old value must actually match that target.
        numeric = r'[0-9零〇一二三四五六七八九十百千万亿两.,，]+(?:元|块|人)?'
        if item_text and any(re.search(r'(不是|并非)' + numeric + r'$', p)
                             and (same_subject(p, item_text) or re.fullmatch(r'(不是|并非)' + numeric, p))
                             and any(value_evidenced(item_text, token) for token in value_tokens(p)) for p in parts):
            chosen.update(index for index, p in enumerate(parts)
                          if re.fullmatch(r'(不是|并非|是|应该是|改成)' + numeric, p))
        for index in list(chosen):
            # Adjacent bare status/condition belongs to this subject; a named new
            # subject never inherits the preceding subject's authority.
            for following in range(index + 1, len(parts)):
                quote = parts[following]
                if not re.match(r'^(也?尚未|也?还没|也?没有|暂时先不|暂时不|先不|未实施|不再用|是[0-9零〇一二三四五六七八九十百千万亿两]|这条保留|该项保留|有效期)', quote):
                    break
                chosen.add(following)
        for index in sorted(chosen):
            quote = parts[index]
            result.append(dict(message_id=message_id, quote=quote, source_type=role,
                               subject=subject_text(item_text or content),
                               negations=[m.group() for m in re.finditer(r'尚未|还没|没有|暂不|先不|不再|取消|除非|如果|只有', quote)]))
    return result


CHANGE_MARKERS = ('改用', '改为', '改成', '换成', '暂不', '先不', '不再', '取消', '作废', '不是', '纠正', '停止')


def split_subject_candidates(candidates, packet):
    """Split independently sourced subjects before checking content or lifetime."""
    prepared = []
    for candidate in candidates:
        parts = clauses(candidate.content)
        groups = []
        for part in parts:
            bindings = bind_evidence(packet, part, source_refs=candidate.source_refs)
            if not bindings:
                groups = []
                break
            sources = {(b['message_id'], b['quote']) for b in bindings}
            if groups and (groups[-1][1] & sources or same_subject(groups[-1][0], part)
                           or re.match(r'^(尚未|还没|没有|也没有|也尚未|不再用)', part)):
                text, known = groups[-1]
                groups[-1] = (text + '，' + part, known | sources)
            else:
                groups.append((part, sources))
        if len(groups) < 2 or getattr(candidate, 'supersedes', ()):
            prepared.append(candidate)
        else:
            prepared.extend(replace(candidate, content=text, stable_key='') for text, _ in groups)
    return prepared


def valid_pending_change(record) -> bool:
    try:
        metadata = json.loads(record.get('metadata') or '{}') if isinstance(record.get('metadata'), str) else record.get('metadata') or {}
        pending = metadata['pending_change']
        bindings = pending['bindings']
        if pending.get('rule_version') in ('model-led-1', 'model-led-2'):
            judgment = pending.get('semantic_judgment') or {}
            return (pending['target_version'] == record.get('record_version')
                and pending['fingerprint'] == fingerprint([record.get('id'),pending['target_version'],bindings])
                and pending.get('source_verified') is True and bool(bindings)
                and judgment.get('change_authorized') is True
                and judgment.get('same_subject') is True
                and all(b.get('message_id') and b.get('quote') and b.get('source_type') == 'user_statement' for b in bindings))
        return (pending['target_version'] == record.get('record_version')
                and pending['fingerprint'] == fingerprint([record.get('id'), pending['target_version'], bindings])
                and bool(bindings) and all(b.get('message_id') and b.get('source_type') == 'user_statement'
                    and same_subject(b['quote'], record.get('content', '')) for b in bindings)
                and any(marker in b['quote'] for b in bindings for marker in CHANGE_MARKERS))
    except (TypeError, ValueError, KeyError):
        return False
