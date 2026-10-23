"""Blink clips in Home Assistant's Media panel: a folder per camera, newest first, playable in place."""

from __future__ import annotations

import re

from blinkpy.auth import UnauthorizedError

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .clips import API, Clips, public_clip
from .const import BLINK_ERRORS, DOMAIN
from .entity import camera_slug

DAYS = 7
# "clip/<id>.mp4?<listing args>": the part after "clip/" is the clip's path under the clips API.
CLIP = re.compile(r"clip/([0-9a-f]{24}\.mp4\?[\w=&%.+-]*)")


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """The Blink clips source."""
    return BlinkClips(hass)


class BlinkClips(MediaSource):
    """Cloud and Sync Module clips, as the app's Clips sheet lists them."""

    name = "Blink clips"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _clips(self) -> Clips:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise BrowseError("Blink Instant Events is not loaded")
        return entries[0].runtime_data.clips

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """The clip's URL; Home Assistant signs it for the player."""
        if not (match := CLIP.fullmatch(item.identifier or "")):
            raise Unresolvable(f"Not a Blink clip: {item.identifier}")
        return PlayMedia(f"{API}/{match[1]}", "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """The cameras, or one camera's clips (``camera/`` alone is every camera's)."""
        identifier = item.identifier or ""
        if identifier.startswith("camera/"):
            return await self._camera(identifier.removeprefix("camera/") or None)
        clips = self._clips()
        cameras = [(camera_slug(cam), cam.name) for cam in clips.blink.cameras.values()]
        return _folder(
            "",
            self.name,
            [_folder("camera/", "All cameras"), *(_folder(f"camera/{slug}", name) for slug, name in cameras)],
        )

    async def _camera(self, slug: str | None) -> BrowseMediaSource:
        clips = self._clips()
        try:
            rows = await clips.async_list(source="both", camera=slug, hours=DAYS * 24, pages=3, limit=100)
        except (UnauthorizedError, *BLINK_ERRORS) as err:
            raise BrowseError(f"Blink did not answer: {err}") from err
        names = {camera_slug(cam): cam.name for cam in clips.blink.cameras.values()}
        children = []
        for clip in rows:
            row = public_clip(clip, {"hours": DAYS * 24, "pages": 3, "limit": 100}, cached=False)
            when = dt_util.as_local(clip["created_at"]).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
            where = "" if slug else f"{clip['camera_name']} · "
            local = " · Sync Module" if clip["source"] == "local" else ""
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"clip/{row['download_url'].removeprefix(API + '/')}",
                    media_class=MediaClass.VIDEO,
                    media_content_type="video/mp4",
                    title=f"{where}{when}{local}",
                    can_play=True,
                    can_expand=False,
                    thumbnail=row["thumbnail_url"],
                )
            )
        title = names.get(slug or "", "All cameras")
        return _folder(f"camera/{slug or ''}", title, children, empty=f"No clips from the last {DAYS} days")


def _folder(
    identifier: str, title: str, children: list[BrowseMediaSource] | None = None, empty: str | None = None
) -> BrowseMediaSource:
    """A directory; an empty one says why in its title."""
    if empty and not children:
        title = f"{title}: {empty}"
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.DIRECTORY,
        media_content_type="",
        title=title,
        can_play=False,
        can_expand=True,
        children=children,
        children_media_class=MediaClass.VIDEO if identifier.startswith("camera/") else MediaClass.DIRECTORY,
    )
