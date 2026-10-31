"""Publish Art Mode as off while keeping modern detection and control live."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.samsungtv_smart.api.art import SamsungTVAsyncArt
from custom_components.samsungtv_smart.api.samsungws import ArtModeStatus, SamsungTVWS
from custom_components.samsungtv_smart.const import (
    DATA_ART_API,
    DATA_IP_CONTROL_STATE_COORDINATOR,
    DEFAULT_APP,
    DOMAIN,
)
from custom_components.samsungtv_smart.media_player import SamsungTVDevice
from custom_components.samsungtv_smart.switch import SamsungTVPowerSwitch
from homeassistant.components.media_player import MediaPlayerState


def _device():
    """Build a powered Frame with the shared Art API and no network calls."""
    device = object.__new__(SamsungTVDevice)
    device._host = "192.0.2.10"
    device._entry_id = "frame"
    device._state = MediaPlayerState.ON
    device._running_app = DEFAULT_APP
    device._ip_art_mode = None
    device._st = None
    device._device_info = {"device": {"PowerState": "on", "FrameTVSupport": "true"}}
    device._ws = SamsungTVWS(device._host)
    device._ws.disable_art_thread()
    device._art_api = SamsungTVAsyncArt(device._host)
    device.hass = SimpleNamespace(
        data={DOMAIN: {device._entry_id: {DATA_ART_API: device._art_api}}}
    )
    device._power_off_in_progress = Mock(return_value=False)
    device._get_ip_control_client = Mock(return_value=None)
    return device


@pytest.mark.parametrize(
    ("event", "field"),
    [("get_artmode_status", "value"), ("art_mode_changed", "status")],
)
async def test_modern_art_events_update_attributes_and_published_state(event, field):
    """Use the active Art API for both entering and leaving Art Mode."""
    device = _device()
    for value, expected in [("on", MediaPlayerState.OFF), ("off", MediaPlayerState.ON)]:
        await device._art_api._process_event(
            "d2d_service_message",
            {"data": json.dumps({"event": event, field: value})},
        )

        assert device.state == expected
        assert device.extra_state_attributes["art_mode_status"] == value
        assert device._state == MediaPlayerState.ON


@pytest.mark.parametrize(
    ("power_state", "art_mode", "expected"),
    [
        (MediaPlayerState.ON, True, MediaPlayerState.OFF),
        (MediaPlayerState.ON, False, MediaPlayerState.ON),
        (MediaPlayerState.ON, None, MediaPlayerState.ON),
        (MediaPlayerState.OFF, True, MediaPlayerState.OFF),
        (MediaPlayerState.OFF, False, MediaPlayerState.OFF),
        (MediaPlayerState.OFF, None, MediaPlayerState.OFF),
    ],
)
def test_state_only_maps_confirmed_art_mode_to_off(power_state, art_mode, expected):
    """Unknown Art Mode must not turn an otherwise powered TV off."""
    device = _device()
    device._state = power_state
    device._art_mode_is_on = Mock(return_value=art_mode)

    assert device.state == expected
    assert device._state == power_state


def test_pending_power_off_still_wins():
    """Retain the immediate feedback after an explicit power-off command."""
    device = _device()
    device._power_off_in_progress.return_value = True

    assert device.state == MediaPlayerState.OFF


@pytest.mark.parametrize("art_on", [True, False])
def test_ip_control_keeps_priority_over_the_art_channel(art_on):
    """Map the shared detector's final verdict rather than its raw WS cache."""
    device = _device()
    device._ip_art_mode = art_on
    device._ip_art_mode_at = None
    device._art_api.art_mode = not art_on

    assert device.state == (MediaPlayerState.OFF if art_on else MediaPlayerState.ON)


@pytest.mark.parametrize(
    ("picture_mode", "expected"),
    [("Ambient", MediaPlayerState.OFF), ("Movie", MediaPlayerState.ON)],
)
def test_panel_snapshot_keeps_priority_over_the_art_channel(picture_mode, expected):
    """Preserve panel-based detection when the legacy Art channel is unused."""
    device = _device()
    device.hass.data[DOMAIN][device._entry_id][DATA_IP_CONTROL_STATE_COORDINATOR] = (
        SimpleNamespace(data={"tv": {"pictureMode": picture_mode}})
    )
    device._art_api.art_mode = picture_mode != "Ambient"

    assert device.state == expected


def test_foreground_app_still_wins_over_a_spurious_art_event():
    """Keep the newer detector's protection against 2024 Frame false events."""
    device = _device()
    device._running_app = "Netflix"
    device._art_api.art_mode = True

    assert device.state == MediaPlayerState.ON


@pytest.mark.parametrize("pending_power_off", [False, True])
async def test_powered_panel_keeps_polling_while_art_is_reported_off(pending_power_off):
    """Continue app visibility and metadata updates while artwork is shown."""
    device = _device()
    device._art_api.art_mode = True
    device._power_off_in_progress.return_value = pending_power_off
    device._auth_method = None
    device._started_up = True
    device._use_mute_check = False
    device._fake_on = None
    device._delayed_set_source = None
    device._log = Mock()
    device._async_load_device_info = AsyncMock(return_value=device._device_info)
    device._get_running_app = Mock()
    device._update_volume_info = AsyncMock()
    device._update_media = AsyncMock()

    await device._async_update()

    assert device.state == MediaPlayerState.OFF
    assert device.extra_state_attributes["art_mode_status"] == "on"
    device._async_load_device_info.assert_any_await(force=True, timeout=None)
    if pending_power_off:
        device._get_running_app.assert_not_called()
        device._update_media.assert_not_awaited()
    else:
        device._get_running_app.assert_called_once_with()
        device._update_media.assert_awaited_once_with()


async def test_turn_on_exits_art_using_modern_detection():
    """Recognize the shared Art API even when the legacy channel is unused."""
    device = _device()
    device._art_api.art_mode = True
    device.async_send_command = AsyncMock(return_value=True)

    assert await device._async_turn_on() is True

    device.async_send_command.assert_awaited_once_with("KEY_POWER")


@pytest.mark.parametrize(
    "legacy_status", [ArtModeStatus.Unsupported, ArtModeStatus.Off]
)
async def test_art_mode_on_is_a_noop_when_already_showing_art(legacy_status):
    """Do not toggle an already active Art Mode out with a power key."""
    device = _device()
    device._art_api.art_mode = True
    device._ws._artmode_status = legacy_status
    device.async_send_command = AsyncMock()

    assert await device._async_power_on(set_art_mode=True) is False

    device.async_send_command.assert_not_awaited()


async def test_legacy_art_mode_service_also_respects_modern_detection():
    """Do not toggle artwork off when set_art_mode is called again."""
    device = _device()
    device._art_api.art_mode = True
    device.async_send_command = AsyncMock()

    await device.async_set_art_mode()

    device.async_send_command.assert_not_awaited()


async def test_source_selection_uses_art_wake_instead_of_cold_power_on():
    """A published off state must not send a second power toggle to artwork."""
    device = _device()
    device._art_api.art_mode = True
    device._source_list = {"HDMI 1": "KEY_HDMI1"}
    device._async_turn_on = AsyncMock()
    device._async_wake_for_source_switch = AsyncMock()
    device._async_send_keys = AsyncMock(return_value=True)
    device._note_source_switch = Mock()
    device._update_media = AsyncMock()
    device.async_write_ha_state = Mock()

    await device.async_select_source("HDMI 1")

    device._async_turn_on.assert_not_awaited()
    device._async_wake_for_source_switch.assert_awaited_once_with("HDMI 1", "KEY_HDMI1")
    device._async_send_keys.assert_awaited_once_with("KEY_HDMI1")


def test_power_switch_stays_on_while_media_player_reports_art_as_off():
    """Keep the existing distinction between viewing state and physical power."""
    device = _device()
    device._art_api.art_mode = True
    power_switch = object.__new__(SamsungTVPowerSwitch)
    power_switch._optimistic_state = None
    power_switch._get_media_player_state = Mock(
        return_value=SimpleNamespace(
            state=device.state, attributes=device.extra_state_attributes
        )
    )

    assert device.state == MediaPlayerState.OFF
    assert power_switch.is_on is True
