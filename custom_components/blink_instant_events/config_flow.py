"""Config flow: Blink email + password, then the 2FA PIN Blink sends."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import ClientError
from blinkpy.auth import Auth, BlinkTwoFARequiredError, LoginError, TokenRefreshFailed
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_PIN
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AUTH, DOMAIN

_LOGGER = logging.getLogger(__name__)


class BlinkInstantEventsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Blink as a separate push client."""

    VERSION = 1

    _auth: Auth

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
