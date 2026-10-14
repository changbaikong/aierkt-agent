import asyncio
import json

import pytest

from scripts.verify_memory_maintenance_loop import FixedReview as MaintenanceReview
from scripts.verify_memory_search_fusion import FixedReview as FusionReview


@pytest.mark.parametrize('provider', [MaintenanceReview, FusionReview])
def test_unknown_offline_scenario_is_never_approved(provider):
    response = asyncio.run(provider().complete([dict(role='user', content=json.dumps({
        'packet': {'turn_id': 'unknown-turn'},
        'items': [{'index': 7, 'candidate': {'content': '未经定义的结论'}}],
    }))]))
    rows = json.loads(response.message['content'])['items']
    assert rows == [{'index': 7, 'verdict': 'unsupported', 'reason': '未知离线样例'}]
