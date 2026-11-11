"""The Blink clip archive: copies each new clip once, waits out live views, removes old clips."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.blink_instant_events.const import CONF_ARCHIVE, CONF_ARCHIVE_DAYS, DOMAIN
from custom_components.blink_instant_events.providers.blink.archive import Archiver, target
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def test_archive(hass: HomeAssistant, tmp_path: Path) -> None:
    """Two clips copied once each, dated by recording time; nothing during a live view; old ones go."""
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_ARCHIVE: True, CONF_ARCHIVE_DAYS: 30})
    entry.add_to_hass(hass)
    now = dt_util.utcnow()
    clips = [
        {"source": "cloud", "slug": "backyard", "camera_name": "Backyard", "created_at": now - timedelta(hours=2)},
        {"source": "local", "slug": "front_door", "camera_name": "Front Door", "created_at": now - timedelta(hours=1)},
    ]
    video = tmp_path / "cached.mp4"
    video.write_bytes(b"mp4")
    store = MagicMock(async_list=AsyncMock(return_value=clips), async_clip=AsyncMock(return_value=video))
    live = MagicMock(sessions={})
    archiver = Archiver(hass, entry, store, live)

    assert await archiver.async_run() == 2
    first = target(archiver.root, clips[0])
    assert first.read_bytes() == b"mp4"
    assert abs(first.stat().st_mtime - clips[0]["created_at"].timestamp()) < 1
    assert await archiver.async_run() == 0  # already there: not fetched again
    assert store.async_clip.await_count == 2

    live.sessions = {"backyard": object()}
    store.async_list.reset_mock()
    assert await archiver.async_run() == 0
    store.async_list.assert_not_awaited()  # the Sync Module isn't asked while someone watches

    old = time.time() - 31 * 86400
    os.utime(first, (old, old))
    live.sessions = {}
    store.async_list.return_value = []
    await archiver.async_run()
    assert not first.exists() and target(archiver.root, clips[1]).exists()
