from __future__ import annotations

import pytest

from agent_core.connection_pool import close_all_pools


@pytest.fixture(autouse=True)
def _close_sqlite_pools() -> None:
    close_all_pools()
    try:
        yield
    finally:
        close_all_pools()
