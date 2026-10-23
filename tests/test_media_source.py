"""Clips in Home Assistant's Media panel."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import media_source
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

ROOT = "media-source://blink_instant_events"
CLIP = {
    "source": "cloud",
    "slug": "backyard",
    "camera_name": "Backyard",
    "created_at": datetime(2026, 10, 22, 18, 5, tzinfo=UTC),
    "size": 1234,
}


async def test_browse_and_play(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """A folder per camera; a clip resolves to the clips API, which Home Assistant signs for the player."""
    assert await async_setup_component(hass, "media_source", {})
    root = await media_source.async_browse_media(hass, ROOT)
    assert [child.title for child in root.children] == ["All cameras", "Backyard", "Front Door"]

    listing = AsyncMock(return_value=[CLIP])
    with patch.object(setup_entry.runtime_data.clips, "async_list", listing):
        folder = await media_source.async_browse_media(hass, f"{ROOT}/camera/backyard")
    assert listing.await_args.kwargs["camera"] == "backyard"
    [clip] = folder.children
    assert clip.can_play and clip.thumbnail.startswith("/api/blink_instant_events/clips/")

    played = await media_source.async_resolve_media(hass, clip.media_content_id, None)
    assert played.url.startswith("/api/blink_instant_events/clips/") and ".mp4?" in played.url
    assert played.mime_type == "video/mp4"


async def test_only_clips_resolve(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """Anything but a clip id is refused, so the source can't be pointed at another path."""
    assert await async_setup_component(hass, "media_source", {})
    for bad in ("clip/../../config.mp4?x", "camera/backyard", "clip/zz.mp4?"):
        try:
            await media_source.async_resolve_media(hass, f"{ROOT}/{bad}", None)
        except media_source.Unresolvable:
            continue
        raise AssertionError(f"{bad} resolved")
