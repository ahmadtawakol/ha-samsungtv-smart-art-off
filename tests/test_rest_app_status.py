import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientConnectionError
import pytest

from custom_components.samsungtv_smart import media_player
from custom_components.samsungtv_smart.api.samsungws import HttpApiError, ResponseError
from custom_components.samsungtv_smart.const import DEFAULT_APP
from custom_components.samsungtv_smart.media_player import SamsungTVDevice
from homeassistant.components.media_player import MediaPlayerState, MediaType

APP_ID = "com.example.player"


@pytest.fixture
def device():
    entity = object.__new__(SamsungTVDevice)
    entity._host = "192.0.2.10"
    entity._log = Mock()
    entity._state = MediaPlayerState.ON
    entity._rest_app_list = {"Video player": APP_ID}
    entity._separate_app_source = True
    entity._running_app = DEFAULT_APP
    entity._app_list = {}
    entity._app_list_st = {}
    entity._st = None
    entity._source_list = {"HDMI input": "IP_HDMI1"}
    entity._get_ip_control_input_source = Mock(return_value="HDMI1")
    entity._ip_art_mode = False
    entity._ip_art_mode_at = None
    entity._power_off_in_progress = Mock(return_value=False)
    entity._rest_api = SimpleNamespace(
        async_rest_app_status=AsyncMock(
            return_value={
                "id": APP_ID,
                "name": "Video player",
                "running": True,
                "visible": True,
            }
        )
    )
    return entity


async def test_foreground_app_has_app_metadata_and_a_separate_hdmi_source(device):
    await device._async_update_rest_app_status()

    device._rest_api.async_rest_app_status.assert_awaited_once_with(
        "com.example.player"
    )
    assert device.app_name == "Video player"
    assert device.app_id == "com.example.player"
    assert device.media_content_type == MediaType.APP
    assert device._get_new_media_title() == "Video player"
    assert device.source == "HDMI input"
    assert device.state == MediaPlayerState.ON


async def test_running_but_hidden_app_is_not_the_current_app(device):
    await device._async_update_rest_app_status()
    device._rest_api.async_rest_app_status.return_value["visible"] = False

    await device._async_update_rest_app_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device._get_new_media_title() == "HDMI input"
    assert device.source == "HDMI input"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        "not supported",
        {},
        {"error": "not found"},
        {"id": "another.app", "visible": True},
        {"id": APP_ID, "running": True},
        {"id": APP_ID, "visible": "true"},
        {"id": APP_ID, "visible": 1},
    ],
)
async def test_missing_or_invalid_status_clears_old_app_metadata(device, reply):
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"
    device._rest_api.async_rest_app_status.return_value = reply

    await device._async_update_rest_app_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device.source == "HDMI input"


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
async def test_failed_poll_does_not_latch_app_or_fail_the_entity(device, error):
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"
    device._rest_api.async_rest_app_status.side_effect = error

    await device._async_update_rest_app_status()

    assert device.app_name is None
    assert device.app_id is None
    assert device.state == MediaPlayerState.ON
    assert device.source == "HDMI input"


async def test_slow_app_endpoint_has_a_bounded_timeout(device, monkeypatch):
    monkeypatch.setattr(media_player, "REST_APP_STATUS_TIMEOUT", 0.01)

    async def silent_endpoint(_app_id):
        await asyncio.Event().wait()

    device._rest_api.async_rest_app_status.side_effect = silent_endpoint
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"

    await asyncio.wait_for(device._async_update_rest_app_status(), timeout=0.5)

    assert device.app_name is None
    assert device.source == "HDMI input"


@pytest.mark.parametrize("power_off_pending", [False, True])
async def test_off_or_powering_off_tv_is_not_probed(device, power_off_pending):
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"
    if power_off_pending:
        device._power_off_in_progress.return_value = True
    else:
        device._state = MediaPlayerState.OFF

    await device._async_update_rest_app_status()

    device._rest_api.async_rest_app_status.assert_not_awaited()
    assert device.app_name is None
    assert device.app_id is None


async def test_visible_app_overrules_stale_art_feedback(device):
    device._ip_art_mode = True

    await device._async_update_rest_app_status()

    assert device._art_mode_is_on() is False
    assert device.state == MediaPlayerState.ON

    device._rest_api.async_rest_app_status.return_value["visible"] = False
    await device._async_update_rest_app_status()

    assert device._art_mode_is_on() is True
    assert device.app_name is None


def test_stale_cloud_art_id_is_not_reported_as_the_current_app(device):
    device._st = SimpleNamespace(channel="", channel_name="art")

    assert device.app_name is None
    assert device.app_id is None


def test_other_native_apps_keep_app_metadata_and_the_physical_input(device):
    device._running_app = "Discovered app"
    device._app_list = {"Discovered app": "com.example.discovered"}

    assert device.app_name == "Discovered app"
    assert device.app_id == "com.example.discovered"
    assert device.source == "HDMI input"
    assert device._get_new_media_title() == "Discovered app"


def test_unmapped_physical_input_is_reported_without_becoming_the_app(device):
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"
    device._get_ip_control_input_source.return_value = "HDMI3"

    assert device.source == "HDMI3"
    assert device.app_name == "Video player"


def test_app_source_fallback_remains_available_without_a_local_input(device):
    device._get_ip_control_input_source.return_value = None
    device._separate_app_source = False
    device._running_app = "Discovered app"

    assert device.source == "Discovered app"


async def test_empty_mapping_disables_all_additional_requests(device):
    device._rest_app_list = {}
    device._rest_app_id = APP_ID
    device._rest_app_name = "Video player"

    await device._async_update_rest_app_status()

    device._rest_api.async_rest_app_status.assert_not_awaited()
    assert device.app_name is None


def test_existing_app_as_source_behavior_can_be_retained(device):
    device._separate_app_source = False
    device._running_app = "Discovered app"

    assert device.source == "Discovered app"


async def test_one_silent_app_does_not_starve_other_configured_apps(
    device, monkeypatch
):
    monkeypatch.setattr(media_player, "REST_APP_STATUS_TIMEOUT", 0.01)
    device._rest_app_list = {"Slow": "com.example.slow", "Ready": "com.example.ready"}

    async def fetch(app_id):
        if app_id == "com.example.slow":
            await asyncio.Event().wait()
        return {"id": app_id, "visible": True}

    device._rest_api.async_rest_app_status.side_effect = fetch
    await asyncio.wait_for(device._async_update_rest_app_status(), timeout=0.5)
    assert device.app_name is None
    await asyncio.wait_for(device._async_update_rest_app_status(), timeout=0.5)

    assert device.app_name == "Ready"
    assert device.app_id == "com.example.ready"
    assert [
        call.args[0] for call in device._rest_api.async_rest_app_status.await_args_list
    ] == ["com.example.slow", "com.example.ready"]


def test_disabled_source_separation_keeps_the_existing_fallback_app_id(device):
    device._separate_app_source = False
    device._rest_app_list = {}
    assert device.app_id == DEFAULT_APP
