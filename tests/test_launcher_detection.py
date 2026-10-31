"""Detect the custom launcher's foreground state separately from HDMI input."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientConnectionError
import pytest

from custom_components.samsungtv_smart import media_player
from custom_components.samsungtv_smart.api.samsungws import HttpApiError, ResponseError
from custom_components.samsungtv_smart.const import DEFAULT_APP
from custom_components.samsungtv_smart.media_player import (
    LAUNCHER_APP_ID,
    SamsungTVDevice,
)
from homeassistant.components.media_player import MediaPlayerState, MediaType


@pytest.fixture
def device():
    """A powered Frame whose physical input is the Apple TV HDMI port."""
    entity = object.__new__(SamsungTVDevice)
    entity._host = "192.0.2.10"
    entity._log = Mock()
    entity._state = MediaPlayerState.ON
    entity._running_app = DEFAULT_APP
    entity._app_list = {}
    entity._app_list_st = {}
    entity._st = None
    entity._source_list = {"Apple TV": "IP_HDMI2"}
    entity._get_ip_control_input_source = Mock(return_value="HDMI2")
    entity._ip_art_mode = False
    entity._ip_art_mode_at = None
    entity._power_off_in_progress = Mock(return_value=False)
    entity._rest_api = SimpleNamespace(
        async_rest_app_status=AsyncMock(
            return_value={
                "id": LAUNCHER_APP_ID,
                "name": "Minimal Hub",
                "running": True,
                "visible": True,
            }
        )
    )
    return entity


async def test_foreground_launcher_has_app_metadata_and_a_separate_hdmi_source(device):
    """Replay the live launcher response without changing the input label."""
    await device._async_update_launcher_status()

    device._rest_api.async_rest_app_status.assert_awaited_once_with("MiniHub001.Hub")
    assert device.app_name == "Launcher"
    assert device.app_id == "MiniHub001.Hub"
    assert device.media_content_type == MediaType.APP
    assert device._get_new_media_title() == "Launcher"
    assert device.source == "Apple TV"
    assert device.state == MediaPlayerState.ON


async def test_running_but_hidden_launcher_is_not_the_current_app(device):
    """A background process must not keep Launcher displayed after HDMI handoff."""
    await device._async_update_launcher_status()
    device._rest_api.async_rest_app_status.return_value["visible"] = False

    await device._async_update_launcher_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device._get_new_media_title() == "Apple TV"
    assert device.source == "Apple TV"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        "not supported",
        {},
        {"error": "not found"},
        {"id": "another.app", "visible": True},
        {"id": LAUNCHER_APP_ID, "running": True},
        {"id": LAUNCHER_APP_ID, "visible": "true"},
        {"id": LAUNCHER_APP_ID, "visible": 1},
    ],
)
async def test_missing_or_invalid_status_clears_old_launcher_metadata(device, reply):
    """Only the requested app's explicit boolean visibility is foreground proof."""
    device._launcher_visible = True
    device._rest_api.async_rest_app_status.return_value = reply

    await device._async_update_launcher_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device.source == "Apple TV"


@pytest.mark.parametrize(
    "error",
    [
        asyncio.TimeoutError(),
        ClientConnectionError("unreachable"),
        HttpApiError("not supported"),
        ResponseError("invalid JSON"),
        ValueError("invalid reply"),
    ],
)
async def test_failed_poll_does_not_latch_launcher_or_fail_the_entity(device, error):
    """A failed app read must not take working power/input reporting down."""
    device._launcher_visible = True
    device._rest_api.async_rest_app_status.side_effect = error

    await device._async_update_launcher_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device.state == MediaPlayerState.ON
    assert device.source == "Apple TV"


async def test_slow_launcher_endpoint_has_a_bounded_timeout(device, monkeypatch):
    """A silent endpoint cannot hold the media player polling loop indefinitely."""
    monkeypatch.setattr(media_player, "LAUNCHER_STATUS_TIMEOUT", 0.01)

    async def silent_endpoint(_app_id):
        await asyncio.Event().wait()

    device._rest_api.async_rest_app_status.side_effect = silent_endpoint
    device._launcher_visible = True

    await asyncio.wait_for(device._async_update_launcher_status(), timeout=0.5)

    assert device.app_name is None
    assert device.source == "Apple TV"


@pytest.mark.parametrize("power_off_pending", [False, True])
async def test_off_or_powering_off_tv_is_not_probed(device, power_off_pending):
    """Do not contact sleeping TVs, or retain an app across a power-off."""
    device._launcher_visible = True
    if power_off_pending:
        device._power_off_in_progress.return_value = True
    else:
        device._state = MediaPlayerState.OFF

    await device._async_update_launcher_status()

    device._rest_api.async_rest_app_status.assert_not_awaited()
    assert device.app_name is None
    assert device.app_id is None


async def test_visible_launcher_overrules_stale_art_feedback(device):
    """Foreground proof follows the existing visible-app Art Mode guard."""
    device._ip_art_mode = True

    await device._async_update_launcher_status()

    assert device._art_mode_is_on() is False
    assert device.state == MediaPlayerState.ON

    device._rest_api.async_rest_app_status.return_value["visible"] = False
    await device._async_update_launcher_status()

    assert device._art_mode_is_on() is True
    assert device.state == MediaPlayerState.OFF
    assert device.app_name is None


def test_stale_cloud_art_id_is_not_reported_as_the_current_app(device):
    """Keep the old SmartThings art latch out of normal viewing app metadata."""
    device._st = SimpleNamespace(channel="", channel_name="art")

    assert device.app_name is None
    assert device.app_id is None


def test_other_native_apps_keep_app_metadata_and_the_physical_input(device):
    """The app name must not replace the separately reported HDMI port."""
    device._running_app = "YouTube"
    device._app_list = {"YouTube": "com.samsung.tv.cobalt-yt"}

    assert device.app_name == "YouTube"
    assert device.app_id == "com.samsung.tv.cobalt-yt"
    assert device.source == "Apple TV"
    assert device._get_new_media_title() == "YouTube"


def test_unmapped_physical_input_is_reported_without_becoming_the_app(device):
    """An unlabelled HDMI input is still separate from the foreground app."""
    device._launcher_visible = True
    device._get_ip_control_input_source.return_value = "HDMI4"

    assert device.source == "HDMI4"
    assert device.app_name == "Launcher"


def test_app_source_fallback_remains_available_without_a_local_input(device):
    """Retain the older source fallback on TVs without an IP Control reading."""
    device._get_ip_control_input_source.return_value = None
    device._running_app = "YouTube"

    assert device.source == "YouTube"
