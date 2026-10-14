"""Tests for runtime event bus."""

import asyncio
from datetime import datetime

import pytest

from agent_core.events import Event, EventType, InMemoryEventBus


@pytest.fixture
def event_bus():
    return InMemoryEventBus()


@pytest.mark.asyncio
async def test_sync_handler(event_bus):
    """Test synchronous event handler."""
    received = []

    def handler(event: Event):
        received.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, handler)

    event = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1", "conversation_id": "conv1"},
        data={"run_id": "run_123"}
    )

    await event_bus.emit(event)

    assert len(received) == 1
    assert received[0].type == EventType.TURN_STARTED
    assert received[0].data["run_id"] == "run_123"


@pytest.mark.asyncio
async def test_async_handler(event_bus):
    """Test asynchronous event handler."""
    received = []

    async def handler(event: Event):
        await asyncio.sleep(0.01)
        received.append(event)

    event_bus.subscribe(EventType.TOOL_COMPLETED, handler)

    event = Event(
        type=EventType.TOOL_COMPLETED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
        data={"tool": "search"}
    )

    await event_bus.emit(event)

    assert len(received) == 1
    assert received[0].data["tool"] == "search"


@pytest.mark.asyncio
async def test_multiple_handlers(event_bus):
    """Test multiple handlers for same event type."""
    received_1 = []
    received_2 = []

    def handler1(event: Event):
        received_1.append(event)

    async def handler2(event: Event):
        received_2.append(event)

    event_bus.subscribe(EventType.TURN_COMPLETED, handler1)
    event_bus.subscribe(EventType.TURN_COMPLETED, handler2)

    event = Event(
        type=EventType.TURN_COMPLETED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )

    await event_bus.emit(event)

    assert len(received_1) == 1
    assert len(received_2) == 1


@pytest.mark.asyncio
async def test_event_filtering(event_bus):
    """Test event filtering by scope."""
    user1_events = []
    user2_events = []

    def user1_handler(event: Event):
        user1_events.append(event)

    def user2_handler(event: Event):
        user2_events.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, user1_handler, owner_id="user1")
    event_bus.subscribe(EventType.TURN_STARTED, user2_handler, owner_id="user2")

    # Emit event for user1
    event1 = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1", "conversation_id": "conv1"},
    )
    await event_bus.emit(event1)

    # Emit event for user2
    event2 = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user2", "conversation_id": "conv2"},
    )
    await event_bus.emit(event2)

    assert len(user1_events) == 1
    assert user1_events[0].scope["owner_id"] == "user1"
    assert len(user2_events) == 1
    assert user2_events[0].scope["owner_id"] == "user2"


@pytest.mark.asyncio
async def test_unsubscribe(event_bus):
    """Test unsubscribing from events."""
    received = []

    def handler(event: Event):
        received.append(event)

    sub_id = event_bus.subscribe(EventType.TURN_STARTED, handler)

    # Emit before unsubscribe
    event1 = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )
    await event_bus.emit(event1)

    # Unsubscribe
    assert event_bus.unsubscribe(sub_id)

    # Emit after unsubscribe
    event2 = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )
    await event_bus.emit(event2)

    # Should only receive first event
    assert len(received) == 1


@pytest.mark.asyncio
async def test_event_type_filtering(event_bus):
    """Test that handlers only receive their subscribed event types."""
    turn_events = []
    tool_events = []

    def turn_handler(event: Event):
        turn_events.append(event)

    def tool_handler(event: Event):
        tool_events.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, turn_handler)
    event_bus.subscribe(EventType.TOOL_STARTED, tool_handler)

    # Emit turn event
    turn_event = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )
    await event_bus.emit(turn_event)

    # Emit tool event
    tool_event = Event(
        type=EventType.TOOL_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
        data={"tool": "search"}
    )
    await event_bus.emit(tool_event)

    assert len(turn_events) == 1
    assert turn_events[0].type == EventType.TURN_STARTED
    assert len(tool_events) == 1
    assert tool_events[0].type == EventType.TOOL_STARTED


@pytest.mark.asyncio
async def test_handler_errors_dont_break_emission(event_bus):
    """Test that handler errors don't prevent other handlers from running."""
    received = []

    def failing_handler(event: Event):
        raise RuntimeError("Handler error")

    def working_handler(event: Event):
        received.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, failing_handler)
    event_bus.subscribe(EventType.TURN_STARTED, working_handler)

    event = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )

    # Should not raise
    await event_bus.emit(event)

    # Working handler should still receive event
    assert len(received) == 1


@pytest.mark.asyncio
async def test_shutdown(event_bus):
    """Test event bus shutdown."""
    received = []

    def handler(event: Event):
        received.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, handler)

    await event_bus.shutdown()

    # Emit after shutdown should be no-op
    event = Event(
        type=EventType.TURN_STARTED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
    )
    await event_bus.emit(event)

    assert len(received) == 0


@pytest.mark.asyncio
async def test_data_filtering(event_bus):
    """Test filtering by event data fields."""
    search_events = []

    def handler(event: Event):
        search_events.append(event)

    event_bus.subscribe(EventType.TOOL_COMPLETED, handler, tool="search")

    # Emit matching event
    event1 = Event(
        type=EventType.TOOL_COMPLETED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
        data={"tool": "search"}
    )
    await event_bus.emit(event1)

    # Emit non-matching event
    event2 = Event(
        type=EventType.TOOL_COMPLETED,
        timestamp=datetime.now(),
        scope={"owner_id": "user1"},
        data={"tool": "memory"}
    )
    await event_bus.emit(event2)

    assert len(search_events) == 1
    assert search_events[0].data["tool"] == "search"
