from agent_core.contracts import Scope
from agent_core.memory_events import MemoryEvent, record_event
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


def test_verified_event_artifact_and_failed_test_keep_formal_provenance(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    try:
        writer = MemoryWriter(repos, Embeddings())
        rows = record_event(writer, Scope('event', 'web', 'c', 'companion'), MemoryEvent(
            event_id='verified-tool-event', state='completed',
            conclusion='产物已生成但测试失败', artifacts=('agent_core/memory_queue.py',),
            evidence=('sha256:abc',), tests_passed=False,
            failure_conditions=('只跑了单测，未跑全量',), failure_reason='断言未通过'))
        contents = {row['content'] for row in rows}
        assert '已生成产物：agent_core/memory_queue.py' in contents
        assert '验证未通过：产物已生成但测试失败（条件：只跑了单测，未跑全量）' in contents
        assert all((row['kind'], row['content_status']) != ('plan', 'completed') for row in rows)
    finally:
        repos.close()
