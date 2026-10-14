"""Real loopback HTTP, isolated storage, independently fixed semantic judgments."""
import asyncio
import json

from scripts import verify_memory_dual_end_http as script


def test_http_round_trip_with_durable_review_recovery_and_idempotency(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('MEMORY_SEMANTIC_ENABLED', raising=False)
    monkeypatch.delenv('MEMORY_PROCESSING_ENDPOINT', raising=False)
    output = tmp_path / 'http-evidence.json'
    result = asyncio.run(script.run(output_path=output))
    report = json.loads(capsys.readouterr().out)
    assert result == 0, [row for row in report['checks'] if not row['passed']]
    assert report['passed'] == report['total'] == 12
    assert report['extractor_calls'] == report['review_fixture_calls'] == 2
    assert report['live_model_calls'] == 0
    assert report['production_memory_touched'] is False
    assert json.loads(output.read_text(encoding='utf-8')) == report
    import os
    assert 'MEMORY_SEMANTIC_ENABLED' not in os.environ
    assert 'MEMORY_PROCESSING_ENDPOINT' not in os.environ


def test_unknown_review_fixture_never_approves():
    provider = script.FixedReviewProvider()
    response = asyncio.run(provider.complete([{'role':'user','content':json.dumps({
        'packet': {'turn_id':'unknown-turn'}, 'items':[{'index':0}]})}]))
    row = json.loads(response.message['content'])['items'][0]
    assert row['verdict'] == 'uncertain'
    assert 'content' not in row
