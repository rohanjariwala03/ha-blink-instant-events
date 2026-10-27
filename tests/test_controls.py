"""The Blink Mini 2's light: only over a live view."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.blink_instant_events import controls

MINI2 = {"slug": "nursery", "name": "Nursery", "product_type": "hawk", "network_id": 1, "id": 2}


class Stream:
    """A live view that takes the light and never reports it back."""

    def __init__(self) -> None:
        self.flood_light = None
        self.set_flood_light = AsyncMock()


@pytest.fixture(autouse=True)
def camera_info():
    with patch.object(controls.blink_api, "request_camera_info", AsyncMock(return_value={"camera": [{}]})):
        yield


async def test_light_over_live_view() -> None:
    stream = Stream()
    assert controls.capabilities("hawk")["flood_light"]
    state = await controls.apply_changes(object(), MINI2, {"flood_light": True}, stream)
    stream.set_flood_light.assert_awaited_once_with(True)
    assert state["flood_light"] is True
    assert state["pending"] == state["rejected"] == []


async def test_light_needs_live_view() -> None:
    with (
        patch.object(controls.blink_api, "request_floodlight") as route,
        pytest.raises(controls.ControlError, match="only during live view"),
    ):
        await controls.apply_changes(object(), MINI2, {"flood_light": True})
    route.assert_not_called()
