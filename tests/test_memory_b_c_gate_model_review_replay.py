import json
from types import SimpleNamespace
from scripts import verify_memory_b_c_gate_replay as script


class ExplicitOfflineReview:
    model = 'explicit-offline-Q17-fixtures'
    async def complete(self,messages,**kwargs):
        payload=json.loads(messages[-1]['content'])
        turn=payload['packet']['turn_id']
        assert turn in ('Q17-1','Q17-2','Q17-3')
        rows=[]
        for item in payload['items']:
            deployment=item['target'].get('id') is not None
            content='部署方案暂时先不部署' if deployment else '排期方案暂不调整'
            rows.append(dict(index=item['index'],verdict='supported',content=content,
                subject='部署方案' if deployment else '排期方案',reason='独立原话核验，删除助手补出的本机地点',
                same_subject=True,preserves_negation=True,preserves_conditions=True,
                change_authorized=deployment,content_status='accepted',
                lifetime={'mode':'phase','basis':'原话暂缓决定，直到用户明确改变'},
                quotes=[{'message_id':turn,'quote':content}]))
        return SimpleNamespace(message={'content':json.dumps({'items':rows},ensure_ascii=False)},usage={})


def test_retained_replay_uses_review_and_retains_evidence(tmp_path,capsys):
    output=tmp_path/'replay.json'
    report=script.run(review_provider=ExplicitOfflineReview(),output=output)
    capsys.readouterr()
    assert report['mode']=='injected_offline_fixture'
    assert report['extraction_model_calls']==0
    assert report['review_model_calls']==3
    assert report['failed_review_calls']==0
    assert report['semantic_acceptance']=='unknown'
    assert len(report['runs'])==3
    for row in report['runs']:
        assert row['pipeline_completed'] is True
        assert row['original_raw'] and row['original_packet']['user_text']
        assert row['review_cache']['completed'] is True
        assert row['old_after']['status']=='archived'
        assert any(x['content']=='部署方案暂时先不部署' for x in row['new_conversation_recall']['warm'])
        assert row['database_before'] and row['database_after']
    assert json.loads(output.read_text(encoding='utf-8'))['semantic_acceptance']=='unknown'


def test_failed_review_calls_counted_without_committing(capsys):
    class Failed:
        async def complete(self,*args,**kwargs):
            raise ConnectionError('offline failure')
    report=script.run(review_provider=Failed())
    capsys.readouterr()
    assert report['review_model_calls']==report['failed_review_calls']==3
    assert all(not row['pipeline_completed'] for row in report['runs'])
    assert all(row['database_before']==row['database_after'] for row in report['runs'])


def test_saved_model_output_replay_uses_no_model_calls_and_keeps_missing_review_incomplete(tmp_path, capsys):
    evidence = script.ROOT / 'docs' / '2026-10-13-memory-model-led-live.json'
    report = script.run(free_replay=True, evidence_path=evidence, output=tmp_path / 'free.json')
    capsys.readouterr()
    assert report['mode'] == 'saved_output_replay'
    assert report['model_calls'] == 0
    assert report['paid_model_calls'] == 0
    assert report['replayed_output_count'] > 0
    assert len(report['runs']) == 8
    rows = {(row['case'], row['repeat']): row for row in report['runs']}
    partial = rows[('Q1', 1)]
    assert not partial['pipeline_completed']
    assert [c['content'] for c in partial['prepared_candidates']] == ['这个项目暂时先不部署']
    assert partial['processing']['pending_review'], 'valid sibling saved while unresolved duration remains visible'
    migrated = rows[('Q3', 2)]
    assert migrated['pipeline_completed']
    assert [c['content'] for c in migrated['prepared_candidates']] == ['用户偏好助手以后回答先说结论']
    assert not migrated['new_conversation_recall']['warm'], 'saved conversation scope cannot pass cross-session acceptance'
    incomplete = {(row['case'], row['repeat']) for row in report['runs'] if not row['pipeline_completed']}
    assert incomplete == {
        ('Q1', 1), ('Q1', 2), ('Q1', 3), ('Q2', 1),
        ('Q2', 2), ('Q2', 3), ('Q3', 1),
    }
    missing = {(row['case'], row['repeat']) for row in report['runs']
               if row['replay_status'] == 'missing_review_output'}
    assert missing == {('Q2', 2), ('Q2', 3)}
    rejected = {(row['case'], row['repeat']) for row in report['runs']
                if row['replay_status'] == 'saved_review_rejected_by_current_protocol'}
    assert rejected == incomplete - missing
    assert report['status_counts'] == {
        'missing_review_output': 2,
        'saved_review_rejected_by_current_protocol': 5,
        'replayed_to_commit_path': 1,
    }
    assert {(row['case'], row['repeat']) for row in report['missing_review_outputs']} == missing
    assert {(row['case'], row['repeat']) for row in report['protocol_rejections']} == rejected
    assert all(row['original_review_raw'] is None for row in report['runs'] if (row['case'], row['repeat']) in missing)
    assert all((row['review_cache'] or {}).get('error') == 'review_not_configured'
               for row in report['runs'] if (row['case'], row['repeat']) in missing)
    assert all(row['model_calls'] == [] for row in report['runs'])
    assert json.loads((tmp_path / 'free.json').read_text(encoding='utf-8'))['semantic_acceptance'] == 'unknown'


def test_three_retained_no_deployment_responses_preserve_success_and_failures(tmp_path, capsys):
    evidence = script.ROOT / 'docs/2026-10-13-memory-b-c-gate-model-review-replay-final.json'
    report = script.run(free_replay=True, evidence_path=evidence, output=tmp_path / 'Q17.json')
    capsys.readouterr()
    assert report['paid_model_calls'] == 0
    assert len(report['runs']) == 3
    first, failed, missing_duration = report['runs']
    assert first['pipeline_completed'], first.get('replay_blockers')
    assert any(r['content'] == '部署方案暂时先不部署' for r in first['new_conversation_recall']['warm'])
    assert not any('立即执行' in r['content'] or '本机' in r['content'] for r in first['new_conversation_recall']['warm'])
    assert failed['replay_status'] == 'missing_review_output'
    assert not failed['pipeline_completed']
    assert missing_duration['replay_status'] == 'saved_review_rejected_by_current_protocol'
    assert not missing_duration['pipeline_completed']
