"""Tests for actions."""

import asyncio
import pytest
import respx
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from unittest.mock import MagicMock, patch

from custom_components.hikvision_next.const import (
    ACTION_EXTRACT_NVR_CLIP,
    ACTION_REBOOT,
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID

@respx.mock
@pytest.mark.parametrize("init_integration", ["DS-7608NXI-I2"], indirect=True)
async def test_extract_nvr_clip_action(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Test extract NVR clip action."""

    entity_id = "camera.ds_7608nxi_i0_0p_s0000000000ccrrj00000000wcvu_101"
    filename = "/tmp/test.mp4"

    with patch("asyncio.create_subprocess_exec") as mock_exec, \
         patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        
        mock_process = MagicMock()
        mock_process.communicate.return_value = asyncio.Future()
        mock_process.communicate.return_value.set_result((b"", b""))
        mock_process.returncode = 0
        mock_exec.return_value = mock_process

        await hass.services.async_call(
            DOMAIN,
            ACTION_EXTRACT_NVR_CLIP,
            {
                ATTR_ENTITY_ID: entity_id,
                "filename": filename,
                "duration": 30,
                "lookback": 10,
            },
            blocking=True,
        )

        assert mock_exec.called
        args = mock_exec.call_args[0]
        # Command should contain rtsp playback url
        assert any("rtsp://" in arg and "starttime=" in arg and "endtime=" in arg for arg in args)
        assert "-t" in args
        assert "30" in args
        assert f"{filename}.tmp" in args
from tests.conftest import TEST_HOST


@respx.mock
@pytest.mark.parametrize("init_integration", ["DS-7608NXI-I2"], indirect=True)
async def test_reboot_action(hass: HomeAssistant, init_integration: MockConfigEntry) -> None:
    """Test sending reboot request on reboot action."""

    mock_config_entry = init_integration

    url = f"{TEST_HOST}/ISAPI/System/reboot"
    endpoint = respx.put(url).respond()

    await hass.services.async_call(
        DOMAIN,
        ACTION_REBOOT,
        {ATTR_CONFIG_ENTRY_ID: mock_config_entry.entry_id},
        blocking=True,
    )

    assert endpoint.called
