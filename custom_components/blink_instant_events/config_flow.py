"""Config flow: Blink email + password, then the 2FA PIN Blink sends."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import ClientError
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed, UnauthorizedError
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_PIN
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import REDIRECT_URI, async_push_sign_in, callback_code, sign_in_request
from .const import CONF_AUTH, CONF_PUSH_AUTH, CONF_SNAPSHOT_AFTER_MOTION, CONF_VISION_URL, DOMAIN

CONF_CALLBACK_URL = "callback_url"

_LOGGER = logging.getLogger(__name__)


class BlinkInstantEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Blink as a separate push client."""

    VERSION = 1

    _auth: Auth

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BlinkOptionsFlow:
        """Options: settings and the push sign-in."""
        return BlinkOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Blink email and password."""
        errors: dict[str, str] = {}
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
            step_id="user",
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
        return await self.async_step_user()

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
        return self.async_create_entry(title=auth["username"], data={CONF_AUTH: auth})


class BlinkOptionsFlow(OptionsFlow):
    """Settings, and the Android sign-in that instant events need."""

    _sign_in: tuple[str, dict[str, str]]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick what to change."""
        return self.async_show_menu(step_id="init", menu_options=["push_sign_in", "settings"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Snapshot after motion costs battery, so it is off until you turn it on. Blink Vision is found by itself."""
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
                    vol.Optional(
                        CONF_VISION_URL, description={"suggested_value": options.get(CONF_VISION_URL)}
                    ): vol.All(str, vol.Match(r"^https?://\S+$")),
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
