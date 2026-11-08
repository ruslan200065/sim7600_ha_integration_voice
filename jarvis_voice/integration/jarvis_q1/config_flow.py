"""Local and Supervisor-discovered setup, no user IP or token needed on HA OS."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .client import JarvisAuthError, JarvisClient, JarvisConnectionError, JarvisError
from .const import CONF_HOST, CONF_PORT, CONF_SOURCE, CONF_TOKEN, DEFAULT_PORT, DOMAIN, UNIQUE_ID
from .helpers import SOURCE_HASSIO, normalize_host, normalize_token

PORT_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


def _connection_schema(defaults: dict[str, Any], *, with_token: bool = True) -> vol.Schema:
    fields = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): PORT_SCHEMA,
    }
    if with_token:
        fields[vol.Required(CONF_TOKEN)] = str
    return vol.Schema(fields)


class JarvisQ1Flow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    def _normalize(self, data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        host = normalize_host(data.get(CONF_HOST))
        if host is None:
            return None, "invalid_host"
        try:
            port = PORT_SCHEMA(data.get(CONF_PORT, DEFAULT_PORT))
        except vol.Invalid:
            return None, "invalid_port"
        token = normalize_token(data.get(CONF_TOKEN))
        if not token:
            return None, "invalid_auth"
        return {CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token}, None

    async def _check(self, data: dict[str, Any]) -> str | None:
        client = JarvisClient(async_get_clientsession(self.hass), data[CONF_HOST],
                              data[CONF_PORT], data[CONF_TOKEN])
        try:
            payload = await client.get("/status", timeout=6)
        except JarvisAuthError:
            return "invalid_auth"
        except JarvisConnectionError:
            return "cannot_connect"
        except JarvisError:
            return "invalid_response"
        if not isinstance(payload, dict) or "call_state" not in payload:
            return "invalid_response"
        return None

    async def _validate(self, raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        data, error = self._normalize(raw)
        if error is None:
            error = await self._check(data)
        return (None, error) if error else (data, None)

    def _entry_for_context(self):
        entry_id = self.context.get("entry_id")
        return self.hass.config_entries.async_get_entry(entry_id) if entry_id else None

    # --- manual setup -------------------------------------------------------
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            data, error = await self._validate(user_input)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(UNIQUE_ID)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="JARVIS Q1", data=data)
        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input or {}), errors=errors)

    # --- Supervisor discovery ----------------------------------------------
    async def async_step_hassio(self, discovery_info: HassioServiceInfo):
        config = discovery_info.config or {}
        data, error = self._normalize({
            CONF_HOST: config.get("host", ""),
            CONF_PORT: config.get("port", DEFAULT_PORT),
            CONF_TOKEN: config.get("token", ""),
        })
        if error:
            return self.async_abort(reason="invalid_discovery_info")
        await self.async_set_unique_id(UNIQUE_ID)
        # Rediscovery after an App restart/token rotation updates the entry and reloads it.
        self._abort_if_unique_id_configured(updates=data)
        self._pending = {**data, CONF_SOURCE: SOURCE_HASSIO}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            error = await self._check(self._pending)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(title="JARVIS Q1 Voice Gateway", data=self._pending)
        return self.async_show_form(step_id="hassio_confirm", data_schema=vol.Schema({}), errors=errors)

    # --- reauth -------------------------------------------------------------
    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._entry_for_context()
        if entry is None:
            return self.async_abort(reason="reauth_failed")
        errors = {}
        if user_input is not None:
            data, error = await self._validate({**entry.data, CONF_TOKEN: user_input.get(CONF_TOKEN)})
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_TOKEN: data[CONF_TOKEN]}, reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            description_placeholders={"host": entry.data.get(CONF_HOST, "")},
            errors=errors,
        )

    # --- reconfigure --------------------------------------------------------
    async def async_step_reconfigure(self, user_input=None):
        entry = self._entry_for_context()
        if entry is None:
            return self.async_abort(reason="reconfigure_failed")
        errors = {}
        if user_input is not None:
            raw = {**user_input}
            if not normalize_token(raw.get(CONF_TOKEN)):
                raw[CONF_TOKEN] = entry.data.get(CONF_TOKEN, "")
            data, error = await self._validate(raw)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, **data}, reason="reconfigure_successful")
        defaults = {**entry.data, **(user_input or {})}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): PORT_SCHEMA,
                vol.Optional(CONF_TOKEN): str,
            }),
            errors=errors,
        )
