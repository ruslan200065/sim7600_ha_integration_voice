"""Local and Supervisor-discovered setup, no user IP or token needed on HA OS."""
import voluptuous as vol
from aiohttp import ClientError, ClientTimeout
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import DOMAIN, CONF_HOST, CONF_PORT, CONF_TOKEN, DEFAULT_PORT


class JarvisQ1Flow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _check(self, data):
        host = data[CONF_HOST]
        if not host or any(ch in host for ch in ("/","\\","@",":"," ","\n")):
            return "invalid_host"
        try:
            async with async_get_clientsession(self.hass).get(
                f"http://{host}:{data[CONF_PORT]}/status",
                headers={"Authorization":"Bearer "+data[CONF_TOKEN]},
                timeout=ClientTimeout(total=6)
            ) as response:
                if response.status != 200:
                    return "cannot_connect"
                payload=await response.json()
                if "call_state" not in payload:
                    return "invalid_response"
        except (OSError,TimeoutError,ClientError,ValueError):
            return "cannot_connect"
        return None

    async def async_step_user(self,user_input=None):
        errors={}
        if user_input is not None:
            if (error:=await self._check(user_input)):
                errors["base"]=error
            else:
                await self.async_set_unique_id("jarvis_q1_voice_bridge")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="JARVIS Q1",data=user_input)
        return self.async_show_form(step_id="user",data_schema=vol.Schema({
            vol.Required(CONF_HOST):str,
            vol.Required(CONF_PORT,default=DEFAULT_PORT):vol.All(vol.Coerce(int),vol.Range(min=1,max=65535)),
            vol.Required(CONF_TOKEN):str,
        }),errors=errors)

    async def async_step_hassio(self,discovery_info:HassioServiceInfo):
        await self._async_handle_discovery_without_unique_id()
        config=discovery_info.config
        self._pending={CONF_HOST:config.get("host",""),
                       CONF_PORT:config.get("port",DEFAULT_PORT),
                       CONF_TOKEN:config.get("token","")}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self,user_input=None):
        errors={}
        if user_input is not None:
            if (error:=await self._check(self._pending)):
                errors["base"]=error
            else:
                await self.async_set_unique_id("jarvis_q1_voice_bridge")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="JARVIS Q1 Voice Gateway",data=self._pending)
        return self.async_show_form(step_id="hassio_confirm",data_schema=vol.Schema({}),errors=errors)
