"""Explicit model judgments for isolated tests; never infer expected meaning."""
import json
from types import SimpleNamespace


class FixedReviewProvider:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={'content': json.dumps({'items': self.rows}, ensure_ascii=False)})


def supported_row(content, quotes, *, index=0, subject='预算', change=True,
                  mode='long_term', days=None, status=None):
    row = dict(index=index, verdict='supported', content=content, subject=subject,
               reason='独立样例预期：该原话明确表达此事项', same_subject=True,
               preserves_negation=True, preserves_conditions=True,
               change_authorized=change,
               lifetime=dict(mode=mode, days=days, basis='该事项的用户原话'),
               quotes=[dict(message_id=message_id, quote=quote) for message_id, quote in quotes])
    if status:
        row['content_status'] = status
    return row


async def prepare_update(writer, packet, suggestion, rows, *, cache=None):
    from agent_core.memory_processing import MemoryReviewProcessor
    durable = []
    _, prepared, result = await MemoryReviewProcessor(writer, FixedReviewProvider(rows)).prepare(
        packet, [], [suggestion], cached=cache, persist=durable.append)
    assert prepared, result.as_dict()
    return prepared[0]
