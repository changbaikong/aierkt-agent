from backend.task_planner import should_track_work_item, work_item_title


def test_tracks_complex_requests_but_not_simple_lookups():
    assert should_track_work_item("帮我实现一个可以持续运行的桌面 Agent 任务账本")
    assert not should_track_work_item("帮我查一下天气")


def test_normalizes_work_item_title():
    assert work_item_title("帮我实现 Agent 任务账本").startswith("实现")
