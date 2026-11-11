"""Config flow: what to add (a Blink account, an RTSP camera, a Home Assistant camera, or where Camera Vision
runs), then that provider's own steps. Blink is email + password, then the 2FA PIN Blink sends; local cameras
need no account anywhere."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed, UnauthorizedError
import voluptuous as vol

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import (
    CONF_AUTH,
    CONF_CAMERA,
    CONF_COOLDOWN,
    CONF_FPS,
    CONF_FRAMES,
    CONF_KEEP_DAYS,
    CONF_KEYFRAMES_ONLY,
    CONF_LIVE_LOOK,
    CONF_MAX_GB,
    CONF_MOTION,
    CONF_POST,
    CONF_PRE,
    CONF_PROVIDER,
    CONF_PUSH_AUTH,
    CONF_RECORD,
    CONF_SENSITIVITY,
    CONF_SNAPSHOT_AFTER_MOTION,
    CONF_VISION_TOKEN,
    CONF_VISION_URL,
    CONF_WIDTH,
    DOMAIN,
    PROVIDER_BLINK,
    PROVIDER_CAMERA,
    PROVIDER_RTSP,
    PROVIDER_VISION,
)
from .providers.blink.api import REDIRECT_URI, async_push_sign_in, callback_code, sign_in_request
from .providers.rtsp import DEFAULTS as RTSP_DEFAULTS
from .providers.rtsp.recorder import MODES
from .providers.rtsp.stream import async_probe as async_probe_rtsp
from .providers.rtsp.url import InvalidUrl, parse as parse_rtsp, with_credentials

VISION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_VISION_URL): vol.All(str, vol.Match(r"^https?://[^\s/]+(:\d+)?/?$")),
        vol.Optional(CONF_VISION_TOKEN): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)


async def async_check_vision(hass: HomeAssistant, url: str, token: str | None) -> str | None:
    """None if Camera Vision answers at `url` and accepts the token, else the error key."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with async_get_clientsession(hass).get(
            f"{url}/people", headers=headers, timeout=ClientTimeout(total=10)
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            return None if resp.status == 200 else "cannot_connect"
    except (ClientError, TimeoutError):
        return "cannot_connect"


def _number(low: float, high: float, step: float = 1, unit: str | None = None) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=step, unit_of_measurement=unit, mode=NumberSelectorMode.BOX)
    )


CONF_CALLBACK_URL = "callback_url"

_LOGGER = logging.getLogger(__name__)


class BlinkInstantEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Blink as a separate push client."""

    VERSION = 1

    _auth: Auth
    _discovered: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Each provider's settings (Blink: also the push sign-in)."""
        return {
            PROVIDER_RTSP: RtspOptionsFlow,
            PROVIDER_CAMERA: CameraOptionsFlow,
            PROVIDER_VISION: VisionOptionsFlow,
        }.get(config_entry.data.get(CONF_PROVIDER, PROVIDER_BLINK), BlinkOptionsFlow)()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What to add."""
        return self.async_show_menu(step_id="user", menu_options=["rtsp", "camera", "blink", "vision"])

    # ------------------------------------------------------------------ Blink

    async def async_step_blink(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Blink email and password."""
        errors: dict[str, str] = {}
        if self.source != SOURCE_REAUTH and any(
            entry.data.get(CONF_PROVIDER, PROVIDER_BLINK) == PROVIDER_BLINK
            for entry in self._async_current_entries(include_ignore=False)
        ):
            return self.async_abort(reason="single_blink_account")
        if user_input is not None:
            self._auth = Auth(
                {"username": user_input[CONF_EMAIL], "password": user_input[CONF_PASSWORD]},
                no_prompt=True,
                session=async_get_clientsession(self.hass),
            )
            try:
                await self._auth.startup()
            except BlinkTwoFARequiredError:
                return await self.async_step_2fa()
            except (LoginError, TokenRefreshFailed):
                errors["base"] = "invalid_auth"
            except (ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error signing in to Blink")
                errors["base"] = "unknown"
            else:
                return await self._async_finish()

        email = self._get_reauth_entry().title if self.source == SOURCE_REAUTH else ""
        return self.async_show_form(
            step_id="blink",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=email): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_2fa(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the PIN Blink sent by SMS or email."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                ok = await self._auth.complete_2fa_login(user_input[CONF_PIN])
            except (ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                if ok:
                    return await self._async_finish()
                errors["base"] = "invalid_pin"
        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Blink rejected the stored session."""
        return await self.async_step_blink()

    async def _async_finish(self) -> ConfigFlowResult:
        if not self._auth.account_id or not self._auth.region_id:
            return self.async_abort(reason="cannot_connect")
        auth = dict(self._auth.login_attributes)
        auth.pop("password", None)
        await self.async_set_unique_id(str(self._auth.account_id))
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates={CONF_AUTH: auth})
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=auth["username"], data={CONF_PROVIDER: PROVIDER_BLINK, CONF_AUTH: auth})

    # ------------------------------------------------------------------ RTSP

    async def async_step_rtsp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """An RTSP address, and the camera's login if it has one; one frame is read to check them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url, username, password = parse_rtsp(user_input[CONF_URL])
            except InvalidUrl as err:
                errors[CONF_URL] = str(err)
            else:
                username = user_input.get(CONF_USERNAME) or username
                password = user_input.get(CONF_PASSWORD) or password
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                ffmpeg = get_ffmpeg_manager(self.hass).binary
                if error := await async_probe_rtsp(ffmpeg, with_credentials(url, username, password), password):
                    errors["base"] = error
                else:
                    data = {CONF_PROVIDER: PROVIDER_RTSP, CONF_URL: url}
                    if username:
                        data |= {CONF_USERNAME: username, CONF_PASSWORD: password or ""}
                    return self.async_create_entry(title=user_input[CONF_NAME], data=data)
        return self.async_show_form(
            step_id="rtsp",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): str,
                        vol.Required(CONF_URL): str,
                        vol.Optional(CONF_USERNAME): str,
                        vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                    }
                ),
                {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    # ------------------------------------------------------- Home Assistant camera

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A camera from another integration (ONVIF, Generic, ...) and the motion sensor that wakes detection."""
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_CAMERA])
            self._abort_if_unique_id_configured()
            state = self.hass.states.get(user_input[CONF_CAMERA])
            title = state.name if state else user_input[CONF_CAMERA]
            return self.async_create_entry(
                title=title,
                data={
                    CONF_PROVIDER: PROVIDER_CAMERA,
                    CONF_CAMERA: user_input[CONF_CAMERA],
                    CONF_MOTION: user_input[CONF_MOTION],
                },
            )
        return self.async_show_form(
            step_id="camera",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CAMERA): EntitySelector(EntitySelectorConfig(domain="camera")),
                    vol.Required(CONF_MOTION): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
                }
            ),
        )

    # ------------------------------------------------------------ Camera Vision

    async def async_step_vision(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Where Camera Vision runs, when it wasn't found by itself (a container, another machine)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_VISION_URL].rstrip("/")
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            if error := await async_check_vision(self.hass, url, user_input.get(CONF_VISION_TOKEN)):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="Camera Vision",
                    data={
                        CONF_PROVIDER: PROVIDER_VISION,
                        CONF_VISION_URL: url,
                        CONF_VISION_TOKEN: user_input.get(CONF_VISION_TOKEN, ""),
                    },
                )
        return self.async_show_form(step_id="vision", data_schema=VISION_SCHEMA, errors=errors)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The add-on announced itself through the Supervisor, with its address and token."""
        config = discovery_info.config
        url = f"http://{config['host']}:{config['port']}"
        await self.async_set_unique_id(discovery_info.uuid)
        self._abort_if_unique_id_configured(updates={CONF_VISION_URL: url, CONF_VISION_TOKEN: config.get("token", "")})
        self.context["title_placeholders"] = {"name": "Camera Vision"}  # the Discovered card's title
        self._discovered = {
            CONF_PROVIDER: PROVIDER_VISION,
            CONF_VISION_URL: url,
            CONF_VISION_TOKEN: config.get("token", ""),
        }
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Use the add-on that announced itself."""
        if user_input is not None:
            return self.async_create_entry(title="Camera Vision", data=self._discovered)
        return self.async_show_form(step_id="hassio_confirm")


class BlinkOptionsFlow(OptionsFlow):
    """Settings, and the Android sign-in that instant events need."""

    _sign_in: tuple[str, dict[str, str]]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick what to change."""
        return self.async_show_menu(step_id="init", menu_options=["push_sign_in", "settings"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Snapshot after motion costs battery, so it is off until you turn it on. Camera Vision is found by itself."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SNAPSHOT_AFTER_MOTION, default=options.get(CONF_SNAPSHOT_AFTER_MOTION, False)
                    ): bool,
                    vol.Required(CONF_LIVE_LOOK, default=options.get(CONF_LIVE_LOOK, False)): bool,
                    vol.Optional(
                        CONF_VISION_URL, description={"suggested_value": options.get(CONF_VISION_URL)}
                    ): vol.All(str, vol.Match(r"^https?://\S+$")),
                    vol.Optional(
                        CONF_VISION_TOKEN, description={"suggested_value": options.get(CONF_VISION_TOKEN)}
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )

    async def async_step_push_sign_in(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Blink's own sign-in page as the Android app; paste back the address it ends on."""
        errors: dict[str, str] = {}
        if user_input is None or not hasattr(self, "_sign_in"):
            self._sign_in = sign_in_request()
        else:
            url, pending = self._sign_in
            if (code := callback_code(user_input[CONF_CALLBACK_URL], pending["state"])) is None:
                errors["base"] = "bad_callback"
            else:
                try:
                    push_auth = await async_push_sign_in(async_get_clientsession(self.hass), code, pending)
                except UnauthorizedError:
                    errors["base"] = "sign_in_expired"
                except (ClientError, TimeoutError, KeyError):
                    errors["base"] = "cannot_connect"
                else:
                    if str(push_auth["account_id"]) != self.config_entry.unique_id:
                        errors["base"] = "wrong_account"
                    else:
                        self.hass.config_entries.async_update_entry(
                            self.config_entry, data={**self.config_entry.data, CONF_PUSH_AUTH: push_auth}
                        )
                        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
                        return self.async_create_entry(data=dict(self.config_entry.options))
            if errors.get("base") == "sign_in_expired":
                self._sign_in = sign_in_request()  # a code works once; start over with a fresh page
        return self.async_show_form(
            step_id="push_sign_in",
            data_schema=vol.Schema({vol.Required(CONF_CALLBACK_URL): str}),
            description_placeholders={"url": self._sign_in[0], "callback": REDIRECT_URI},
            errors=errors,
        )


class RtspOptionsFlow(OptionsFlow):
    """How the stream is read, how motion is judged, and what is recorded."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Straight to the form."""
        return await self.async_step_rtsp()

    async def async_step_rtsp(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = RTSP_DEFAULTS | dict(self.config_entry.options)
        schema = vol.Schema(
            {
                vol.Required(CONF_FPS): _number(0.2, 10, 0.1, "fps"),
                vol.Required(CONF_WIDTH): _number(160, 1920, 1, "px"),
                vol.Required(CONF_KEYFRAMES_ONLY): bool,
                vol.Required(CONF_SENSITIVITY): _number(0.1, 50, 0.1, "%"),
                vol.Required(CONF_FRAMES): _number(1, 10),
                vol.Required(CONF_COOLDOWN): _number(0, 600, 1, "s"),
                vol.Required(CONF_RECORD): SelectSelector(
                    SelectSelectorConfig(options=list(MODES), translation_key=CONF_RECORD)
                ),
                vol.Required(CONF_PRE): _number(0, 30, 1, "s"),
                vol.Required(CONF_POST): _number(1, 120, 1, "s"),
                vol.Required(CONF_KEEP_DAYS): _number(1, 365, 1, "d"),
                vol.Required(CONF_MAX_GB): _number(0.5, 2000, 0.5, "GB"),
            }
        )
        return self.async_show_form(step_id="rtsp", data_schema=self.add_suggested_values_to_schema(schema, current))


class CameraOptionsFlow(OptionsFlow):
    """How many pictures an event gets, and how often."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Straight to the form."""
        return await self.async_step_camera()

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = {CONF_FRAMES: 5, CONF_COOLDOWN: 30} | dict(self.config_entry.options)
        schema = vol.Schema(
            {vol.Required(CONF_FRAMES): _number(1, 10), vol.Required(CONF_COOLDOWN): _number(0, 600, 1, "s")}
        )
        return self.async_show_form(step_id="camera", data_schema=self.add_suggested_values_to_schema(schema, current))


class VisionOptionsFlow(OptionsFlow):
    """A new address or token."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Straight to the form."""
        return await self.async_step_vision()

    async def async_step_vision(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Checked before it is kept."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_VISION_URL].rstrip("/")
            if error := await async_check_vision(self.hass, url, user_input.get(CONF_VISION_TOKEN)):
                errors["base"] = error
            else:
                return self.async_create_entry(data={**user_input, CONF_VISION_URL: url})
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="vision",
            data_schema=self.add_suggested_values_to_schema(VISION_SCHEMA, {CONF_VISION_URL: current[CONF_VISION_URL]}),
            errors=errors,
        )
