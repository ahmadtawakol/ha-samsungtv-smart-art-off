import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.samsungtv_smart.media_player import SamsungTVDevice

from .test_external_audio_feedback import device


def bind_shared(entity, value="external", age=0):
    del entity._speaker_output_is_internal
    entity._speaker_output_state = MagicMock(return_value=None)
    entity._active_shared_ip_entry = lambda: SimpleNamespace(options={})
    coordinator = SimpleNamespace(
        last_update_success=True,
        data={"tv": {"speakerSelect": value}, "polled_at": time.monotonic() - age},
    )
    entity._get_ip_control_state_coordinator = lambda: coordinator
    return coordinator


async def test_fresh_shared_external_output_works_before_selector_discovery(device):
    bind_shared(device)
    await device._update_volume_info()
    assert device.volume_level == 0.42
    device._speaker_output_state.assert_not_called()
    device._get_ip_control_client.assert_not_called()
    device._upnp.async_get_volume.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("internal", True), ("External", False), ("AudioOut/Optical", False)],
)
def test_canonical_shared_output_is_used_directly(device, value, expected):
    bind_shared(device, value)
    assert device._speaker_output_is_internal() is expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        False,
        0,
        {},
        "",
        "unknown",
        "unavailable",
        "null",
        "unrecognized-device-id",
    ],
)
def test_invalid_or_unresolved_shared_values_keep_the_selector_fallback(device, value):
    bind_shared(device, value)
    device._speaker_output_state.return_value = "Internal"
    assert device._speaker_output_is_internal() is True
    device._speaker_output_state.assert_called_once()


@pytest.mark.parametrize(
    "problem", ["stale", "future", "failed", "powered_off", "disabled"]
)
def test_unusable_snapshot_does_not_override_the_output_selector(device, problem):
    coordinator = bind_shared(device)
    if problem == "stale":
        coordinator.data["polled_at"] -= 100
    if problem == "future":
        coordinator.data["polled_at"] += 100
    if problem == "failed":
        coordinator.last_update_success = False
    if problem == "powered_off":
        coordinator.data["powered_off"] = True
    if problem == "disabled":
        device._active_shared_ip_entry = lambda: None
    device._speaker_output_state.return_value = "Internal"
    assert device._speaker_output_is_internal() is True


async def test_unknown_output_shows_unknown_audio_without_querying_tv_volume(device):
    bind_shared(device, None)
    device._attr_volume_level = 0.0
    await device._update_volume_info()
    assert device.volume_level is None
    assert device.is_volume_muted is None
    device._upnp.async_get_volume.assert_not_awaited()
    device._get_ip_control_client.assert_not_called()


def test_shared_output_event_applies_feedback_before_one_publication(device):
    bind_shared(device)
    device._apply_shared_panel_art_state = MagicMock(return_value=True)
    device._shared_ip_needs_publish = MagicMock(return_value=True)
    device._attr_volume_level = 0.0
    device._shared_ip_updated()
    assert device.volume_level == 0.42
    device.async_write_ha_state.assert_called_once()


async def test_art_exit_callback_does_not_publish_an_assumed_zero(device, hass):
    bind_shared(device)
    device._attr_volume_level = 0.0
    device._st = None
    device._refresh_ip_art_mode = AsyncMock()
    published = []
    device.async_write_ha_state.side_effect = lambda: published.append(
        device.volume_level
    )
    device._on_art_transition()
    await hass.async_block_till_done()
    assert published == [0.42]


def test_initial_link_configuration_clears_the_legacy_zero_default(device):
    device._external_audio_entity = None
    device._attr_volume_level = 0.0
    device._speaker_output_is_internal.return_value = None
    device._get_option = lambda name, default=None: (
        "media_player.test_receiver" if name == "external_audio_entity" else default
    )
    device._load_tv_lists = MagicMock()
    device._app_list = {}
    device._ws = MagicMock()
    device._update_config_options(first_load=True)
    assert device.volume_level is None
    assert device.is_volume_muted is None


def test_public_properties_are_correct_before_any_audio_update_runs(device):
    bind_shared(device)
    device._attr_volume_level = 0.0
    device._attr_is_volume_muted = True
    assert device.volume_level == 0.42
    assert device.is_volume_muted is False


@pytest.mark.parametrize("output", ["internal", "external"])
def test_ambient_snapshot_cannot_supply_a_normal_viewing_output(device, output):
    coordinator = bind_shared(device, output)
    coordinator.data["tv"]["pictureMode"] = "Ambient"
    device._attr_volume_level = 0.0
    assert device._speaker_output_is_internal() is None
    assert device.volume_level is None
    assert device.is_volume_muted is None


def test_a_real_receiver_zero_is_still_reported(device, hass):
    bind_shared(device)
    hass.states.async_set(
        device._external_audio_entity,
        "playing",
        {
            "volume_level": 0.0,
            "is_volume_muted": False,
        },
    )
    assert device.volume_level == 0.0


def test_known_internal_speakers_keep_their_actual_zero(device):
    bind_shared(device, "internal")
    device._attr_volume_level = 0.0
    assert device.volume_level == 0.0


async def test_repeated_mute_request_keeps_the_existing_toggle_guard(device):
    device.async_send_command = AsyncMock(return_value=True)
    await device.async_mute_volume(True)
    await device.async_mute_volume(True)
    device.async_send_command.assert_awaited_once_with("KEY_MUTE")


async def test_immediate_unmute_is_not_lost_while_receiver_feedback_catches_up(device):
    device.async_send_command = AsyncMock(return_value=True)
    await device.async_mute_volume(True)
    await device.async_mute_volume(False)
    assert device.async_send_command.await_count == 2
    assert device._attr_is_volume_muted is False
