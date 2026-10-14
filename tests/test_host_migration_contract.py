from fastapi.routing import APIRoute


def test_legacy_worker_routes_are_removed():
    import web_api

    paths = {route.path for route in web_api.app.routes if isinstance(route, APIRoute)}
    assert "/api/work/run" not in paths
    assert "/api/work/status" not in paths
    assert "/api/work/mode" not in paths
    assert "/api/work/events" not in paths
    assert "/api/work/volume" not in paths
    assert "/api/work/screenshot" not in paths


def test_work_task_request_requires_explicit_task_type_and_input():
    from web_api import WorkTaskRequest

    request = WorkTaskRequest(
        task_type="file.list",
        objective="列出文件",
        input={"directory": "C:/workspace", "sort_by": "size"},
    )
    assert request.task_type == "file.list"
    assert request.input["sort_by"] == "size"


def test_work_capabilities_route_exists():
    import web_api

    paths = {route.path for route in web_api.app.routes if isinstance(route, APIRoute)}
    assert "/api/work/capabilities" in paths
