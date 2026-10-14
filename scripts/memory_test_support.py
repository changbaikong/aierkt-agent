"""Explicit seed entry for isolated tests and acceptance probes, never production."""
from dataclasses import replace


def seed_semantic(writer, scope, candidates, **kwargs):
    candidates = list(candidates)
    if kwargs.get('packet') is not None:
        return writer.save_semantic_candidates(scope, candidates, **kwargs)
    # Explicit synthetic source exists only here, outside all product callers.
    from datetime import UTC, datetime
    from agent_core.memory_turn import turn_from_messages
    turn = kwargs.get('turn_id') or 'isolated-seed'
    packet = turn_from_messages(turn_id=turn,occurred_at=kwargs.get('occurred_at') or datetime.now(UTC).isoformat(),
        scope=scope,user_text='。'.join(c.content for c in candidates),published_reply='')
    return writer.save_semantic_candidates(scope,[replace(c,source_refs=(turn,),origin='model_candidate') for c in candidates],packet=packet,**kwargs)
