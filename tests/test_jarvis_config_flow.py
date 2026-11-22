"""Exercise JARVIS companion discovery with Home Assistant's config flow."""

from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from jarvis_voice.integration.jarvis_q1.config_flow import JarvisQ1Flow
from jarvis_voice.integration.jarvis_q1.const import DOMAIN, UNIQUE_ID


def discovery() -> HassioServiceInfo:
    """Return discovery as sent by an older add-on with an invalid host."""
    return HassioServiceInfo(
        config={"host": "invalid/hostname", "port": 8099, "token": "test"},
        name="JARVIS",
        slug="abcd_jarvis_sim7600_voice",
        uuid="test",
    )


async def test_discovery_can_use_supervisor_slug(hass: HomeAssistant) -> None:
    """Recover the internal address when an older add-on publishes a bad host."""
    flow = JarvisQ1Flow()
    flow.hass = hass
    flow.context = {"source": "hassio"}
    flow.async_set_unique_id = AsyncMock()

    result = await flow.async_step_hassio(discovery())

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert flow._pending["host"] == "abcd-jarvis-sim7600-voice"


async def test_discovery_does_not_replace_manual_settings(hass: HomeAssistant) -> None:
    """Keep manually selected connection details when the same app is discovered."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIQUE_ID,
        data={"host": "manual.local", "port": 8099, "token": "manual"},
    )
    entry.add_to_hass(hass)
    flow = JarvisQ1Flow()
    flow.hass = hass
    flow.context = {"source": "hassio"}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = Mock(
        side_effect=AbortFlow("already_configured")
    )
    with pytest.raises(AbortFlow, match="already_configured"):
        await flow.async_step_hassio(discovery())
    flow._abort_if_unique_id_configured.assert_called_once_with()
    assert entry.data["host"] == "manual.local"
