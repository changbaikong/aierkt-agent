"""Model-led judgments; source/integrity checks never infer language meaning."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
import json
import re
import secrets

from .memory_binding import fingerprint, unique_adopted_proposal
from .memory_turn import clauses, value_tokens, value_entries, value_evidenced

RULE_VERSION = 'model-led-2'


def literal(text):
    """Formatting and equivalent numeric spelling only. Keep relationship words."""
    raw = str(text or '')
    for token in sorted(value_tokens(raw), key=len, reverse=True):
        values = value_entries(token)
        if values:
            raw = raw.replace(token, str(values[0][0]))
    return re.sub(r'\s|[，,。；;！!？?]', '', raw)


def original_sources(packet):
    if packet is None:
        return []
    sources = []
    def add(message_id, text, role):
        for quote in clauses(text):
            sources.append(dict(message_id=message_id, quote=quote, source_type=role))
    add(packet.turn_id, packet.user_text, 'user_statement')
    for history in packet.relevant_history:
        if history.role in ('user', 'assistant'):
            add(history.message_id, history.content, 'user_statement' if history.role == 'user' else 'assistant_proposal')
    for ref in packet.evidence_refs:
        if ref.outcome in ('success', 'succeeded', 'completed'):
            add(ref.ref_id, ref.summary, 'tool_result')
    return sources


def exact_bindings(item, packet):
    """Copying a whole original clause is not a paraphrase or role inference."""
    wanted = set(item.source_refs)
    available = [s for s in original_sources(packet)
                 if s['source_type'] in ('user_statement', 'tool_result') and (not wanted or s['message_id'] in wanted)]
    adopted = unique_adopted_proposal(packet) if packet else None
    if adopted:
        # Only an entire single proposal plus explicit acknowledgement may use
        # this exact-copy fast path; semantic variants still need the model.
        quote = adopted[1].strip().rstrip('？?')
        quote = re.sub(r'(可以吗|好吗)$', '', quote)
        available.append(dict(message_id=adopted[0],quote=quote,source_type='adopted_proposal'))
    bindings = []
    for claim in clauses(item.content):
        match = next((s for s in available if literal(claim) == literal(s['quote'])), None)
        if match is None:
            return []
        bindings.append({**match, 'subject':claim, 'negations':[]})
    return bindings


def hard_gaps(content, sources, current=''):
    """Only explicit absent values / exact labelled numeric contradictions.

    Different lexical descriptions of a subject are a model responsibility.
    Never borrow the union of numbers to prove a labelled field's new value.
    """
    texts = [s['quote'] if isinstance(s, dict) else str(s) for s in sources]
    gaps = []
    for claim in clauses(content):
        if any(literal(claim) == literal(old) for old in clauses(current)):
            continue
        # Unknown wording is not evidence of absence. Approval/execution meaning
        # belongs to the agent; only a literal polarity flip is provable here.
        negative = r'(?:尚未|还没有|还没|没有|暂不|先不|未|不)'
        for quote in texts:
            before, after = literal(quote), literal(claim)
            if before != after and re.sub(negative, '', before) == re.sub(negative, '', after):
                if bool(re.search(negative, before)) != bool(re.search(negative, after)):
                    # History may contain the very decision being corrected.
                    # An exact current/source statement of the new claim makes
                    # this a semantic change for the model, not a provable flip.
                    if not any(literal(source) == after for source in texts):
                        gaps.append(dict(category='polarity',item=claim,clause=claim,verdict='unsupported',quote=quote,reason='完整原句的否定被直接反转'))
        for token in value_tokens(claim):
            if not value_entries(token):
                continue
            if not any(value_evidenced(quote, token) for quote in texts):
                gaps.append(dict(category='value',item=token,clause=claim,verdict='unsupported',quote='',reason='原始依据没有该数值'))
        # Compare explicit label+number, without role guessing or similarity.
        # e.g. 项目预算3000 cannot use 广告预算9000 as its numeric source.
        value_pattern = r'([\u3400-\u9fffA-Za-z]+?)(?:为|是)?(\d+(?:\.\d+)?)(元|人|天|次)?$'
        matched = re.fullmatch(value_pattern, literal(claim))
        if not matched:
            continue
        label = re.sub(r'(?:为|是)$', '', matched.group(1))
        same_label = []
        for quote in texts:
            other = re.fullmatch(value_pattern, literal(quote))
            if other and re.sub(r'(?:为|是)$', '', other.group(1)) == label:
                same_label.append(other)
        if same_label and not any(m.group(2) == matched.group(2) and (not m.group(3) or not matched.group(3) or m.group(3)==matched.group(3)) for m in same_label):
            gaps.append(dict(category='value',item=matched.group(2),clause=claim,verdict='unsupported',quote='',reason='同一明确字段的数值与原文冲突'))
    return gaps


def judgment_of(record):
    """Agent-visible reasoning, not a signing key or authority claim."""
    try:
        metadata = record.get('metadata') or {}
        if isinstance(metadata,str):
            metadata = json.loads(metadata)
        judgment = (metadata.get('evidence_receipt') or {}).get('semantic_judgment') or {}
        return {k:judgment[k] for k in ('verdict','content','subject','reason','quotes','content_status','lifetime','change_authorized') if k in judgment}
    except (ValueError,TypeError,AttributeError):
        return {}


def reviewed_lifetime(authority, item, packet, target=None):
    """Interpret only a service-verified agent decision, never candidate words."""
    from datetime import timedelta
    from .memory_horizon import Lifetime, LONG_TERM, SHORT_TERM
    proof = authority.verify(item,packet,target)
    if not proof:
        return None
    decision = proof['semantic_judgment'].get('lifetime') or {}
    mode = decision.get('mode')
    basis = decision.get('basis')
    if mode not in ('long_term','short_term','phase') or not isinstance(basis,str) or not basis.strip():
        return None
    if mode == 'short_term':
        days = decision.get('days')
        if type(days) is not int or not 0 < days <= 365000:
            return None
        return Lifetime(SHORT_TERM,timedelta(days=days),basis)
    return Lifetime(LONG_TERM,None,basis)


def review_context(item, packet, target):
    payload = asdict(item)
    payload.pop('review_receipt', None)
    payload.pop('stable_key', None)
    # Scope is separately checked against the trusted packet at commit.
    for name in ('project_id','project_label','scope_kind'):
        payload.pop(name, None)
    return dict(item=payload, packet=packet.as_payload() if packet else None,
                target={k:(target or {}).get(k) for k in ('id','content','record_version','owner_id','channel','module_id','scope_kind','project_id')},rule_version=RULE_VERSION)


class MemoryReviewAuthority:
    """Local service key prevents model/HTTP data from self-authorizing writes.

    Stored only in the local record DB; never part of a model or sync payload.
    Reopened writers use the same key to verify durable completed decisions.
    """
    def __init__(self, repositories):
        self.repositories = repositories
        self._key = None

    def _secret(self):
        if self._key is None:
            with self.repositories.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS memory_review_authority (singleton INTEGER PRIMARY KEY CHECK(singleton=1), secret BLOB NOT NULL)')
                db.execute('INSERT OR IGNORE INTO memory_review_authority VALUES (1, ?)', (secrets.token_bytes(32),))
                self._key = bytes(db.execute('SELECT secret FROM memory_review_authority WHERE singleton=1').fetchone()[0])
        return self._key

    def _signature(self, payload):
        return hmac.new(self._secret(), json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str).encode(),hashlib.sha256).hexdigest()

    def issue(self, item, packet, target, judgment, *, key, raw):
        payload = dict(context_fingerprint=fingerprint(review_context(item,packet,target)),
                       semantic_judgment=judgment, review_key=key, raw=raw, rule_version=RULE_VERSION)
        return {**payload,'signature':self._signature(payload)}

    def verify(self, item, packet, target):
        proof = getattr(item,'review_receipt',None)
        if not isinstance(proof,dict) or not proof.get('signature'):
            return None
        payload = {k:v for k,v in proof.items() if k != 'signature'}
        if payload.get('context_fingerprint') != fingerprint(review_context(item,packet,target)):
            return None
        if not hmac.compare_digest(str(proof['signature']),self._signature(payload)):
            return None
        judgment = payload.get('semantic_judgment')
        return payload if isinstance(judgment,dict) and judgment.get('verdict') == 'supported' else None
