import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.samsungtv_smart.api.samsungws import ArtModeStatus
from custom_components.samsungtv_smart.art_mode_state import (
    art_broadcast_override,
    shared_panel_art_mode,
)
from custom_components.samsungtv_smart.const import (
    CONF_ENABLE_IP_CONTROL,
    CONF_IP_CONTROL_TOKEN,
    DATA_ART_API,
    DATA_IP_CONTROL_STATE_COORDINATOR,
    DEFAULT_APP,
    DOMAIN,
)
from custom_components.samsungtv_smart.media_player import SamsungTVDevice
from homeassistant.components.media_player import MediaPlayerState


class SharedCoordinator:
    def __init__(self, mode="Movie", observed_at=None):
        self.data = {
            "powered_off": False,
            "tv": {"pictureMode": mode},
            "polled_at": time.monotonic() if observed_at is None else observed_at,
        }
        self.last_update_success = True
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def remove():
            if listener in self.listeners:
                self.listeners.remove(listener)

        return remove

    def emit(self, mode):
        self.data["tv"]["pictureMode"] = mode
        self.data["polled_at"] = time.monotonic()
        for listener in tuple(self.listeners):
            listener()


@pytest.fixture
def device():
    entry = SimpleNamespace(
        entry_id="example",
        data={CONF_IP_CONTROL_TOKEN: "synthetic"},
        options={CONF_ENABLE_IP_CONTROL: True},
    )
    coordinator = SharedCoordinator()
    art = SimpleNamespace(art_mode_last_broadcast=None, art_mode=True)
    entity = object.__new__(SamsungTVDevice)
    entity._log = Mock()
    entity._entry_id = entry.entry_id
    entity._state = MediaPlayerState.ON
    entity._running_app = DEFAULT_APP
    entity._device_info = {"device": {"FrameTVSupport": "true", "PowerState": "on"}}
    entity._ws = SimpleNamespace(artmode_status=ArtModeStatus.Unsupported)
    entity._ip_art_mode = True
    entity._ip_art_mode_at = time.monotonic() - 60
    entity._st = None
    entity.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda _: entry),
        data={
            DOMAIN: {
                entry.entry_id: {
                    DATA_IP_CONTROL_STATE_COORDINATOR: coordinator,
                    DATA_ART_API: art,
                }
            }
        },
    )
    entity._get_ip_control_client = Mock(return_value=None)
    entity.async_write_ha_state = Mock()
    return entity


def test_shared_mode_replaces_an_older_ip_art_cache(device):
    assert device._art_mode_is_on() is False
    device._get_ip_control_client.assert_not_called()


def test_visible_native_app_still_overrules_a_spurious_art_reading(device):
    device._running_app = "Example app"
    device._get_ip_control_state_coordinator().data["tv"]["pictureMode"] = "Ambient"
    assert device._art_mode_is_on() is False


def test_non_frame_ambient_mode_is_not_classified_as_art(device):
    device._device_info["device"]["FrameTVSupport"] = "false"
    device._ip_art_mode = None
    device.hass.data[DOMAIN][device._entry_id].pop(DATA_ART_API)
    device._get_ip_control_state_coordinator().data["tv"]["pictureMode"] = "Ambient"
    assert device._shared_panel_art_state() is None
    assert device._art_mode_is_on() is None


async def test_regular_refresh_reuses_the_shared_reply_without_an_ip_request(device):
    await device._refresh_ip_art_mode_once()
    assert device._ip_art_mode is False
    assert (
        device._ip_art_mode_at
        == device._get_ip_control_state_coordinator().data["polled_at"]
    )
    device._get_ip_control_client.assert_not_called()


def test_snapshot_updates_publish_both_directions_without_network_requests(device):
    coordinator = device._get_ip_control_state_coordinator()
    device._attach_shared_ip_listener()
    assert device._art_mode_is_on() is False
    coordinator.emit("Ambient")
    assert device._art_mode_is_on() is True
    coordinator.emit("Movie")
    assert device._art_mode_is_on() is False
    assert device.async_write_ha_state.call_count == 3
    device._get_ip_control_client.assert_not_called()


def test_listener_attaches_after_sensor_setup_without_duplicates(device):
    data = device.hass.data[DOMAIN][device._entry_id]
    coordinator = data.pop(DATA_IP_CONTROL_STATE_COORDINATOR)
    device._attach_shared_ip_listener()
    assert coordinator.listeners == []
    data[DATA_IP_CONTROL_STATE_COORDINATOR] = coordinator
    device._attach_shared_ip_listener()
    device._attach_shared_ip_listener()
    assert len(coordinator.listeners) == 1


def test_replacement_and_cleanup_remove_old_subscriptions(device):
    first = device._get_ip_control_state_coordinator()
    device._attach_shared_ip_listener()
    second = SharedCoordinator("Ambient")
    device.hass.data[DOMAIN][device._entry_id][
        DATA_IP_CONTROL_STATE_COORDINATOR
    ] = second
    device._attach_shared_ip_listener()
    assert first.listeners == []
    assert len(second.listeners) == 1
    device._detach_shared_ip_listener()
    device._detach_shared_ip_listener()
    assert second.listeners == []


def test_disabling_ip_control_stops_consuming_the_old_snapshot(device):
    coordinator = device._get_ip_control_state_coordinator()
    device._attach_shared_ip_listener()
    device.hass.config_entries.async_get_entry(device._entry_id).options[
        CONF_ENABLE_IP_CONTROL
    ] = False
    device._attach_shared_ip_listener()
    assert coordinator.listeners == []
    assert device._shared_panel_art_state() is None


def test_recent_mode_event_can_lead_the_shared_snapshot(device):
    art = device.hass.data[DOMAIN][device._entry_id][DATA_ART_API]
    art.art_mode_last_broadcast = (True, time.monotonic() - 1)
    assert device._art_mode_is_on() is True
    art.art_mode_last_broadcast = (True, time.monotonic() - 30)
    assert device._art_mode_is_on() is False


def test_no_observation_does_not_need_to_read_mode_events(device):
    device._latest_art_broadcast = Mock(side_effect=AssertionError)
    assert device._art_broadcast_overrides(None) is None
    device._latest_art_broadcast.assert_not_called()


def test_an_older_snapshot_cannot_undo_a_confirmed_power_off(device):
    coordinator = device._get_ip_control_state_coordinator()
    device._ip_art_mode = False
    device._ip_art_mode_at = None
    device._ip_power_off_observed_at = coordinator.data["polled_at"] + 1
    assert device._shared_panel_art_state() is None
    assert device._art_mode_is_on() is False
    coordinator.data["polled_at"] = time.monotonic()
    device._ip_power_off_observed_at = coordinator.data["polled_at"] - 1
    assert device._apply_shared_panel_art_state() is True


@pytest.mark.parametrize("mode", [None, "", "unknown", "unavailable", "null", "none"])
def test_unknown_mode_values_are_not_interpreted_as_normal_tv(mode):
    coordinator = SharedCoordinator(mode, observed_at=100.0)
    assert shared_panel_art_mode(coordinator, now=100.0, poll_interval=10) is None


@pytest.mark.parametrize(
    "observed_at", [None, "100", True, float("nan"), float("inf"), 200]
)
def test_invalid_or_future_timestamps_are_unreliable(observed_at):
    coordinator = SharedCoordinator()
    coordinator.data["polled_at"] = observed_at
    assert shared_panel_art_mode(coordinator, now=100.0, poll_interval=10) is None


def test_cached_state_requires_a_successful_recent_response():
    coordinator = SharedCoordinator("Ambient", observed_at=100.0)
    assert shared_panel_art_mode(coordinator, now=100.0, poll_interval=10) == (
        True,
        100,
    )
    assert shared_panel_art_mode(coordinator, now=131.0, poll_interval=10) is None
    coordinator.last_update_success = False
    assert shared_panel_art_mode(coordinator, now=100.0, poll_interval=10) is None


def test_normal_mode_is_a_real_false_value_not_an_unavailable_reading():
    coordinator = SharedCoordinator("Movie", observed_at=100.0)
    assert shared_panel_art_mode(coordinator, now=100.0, poll_interval=10) == (
        False,
        100,
    )


def test_power_off_and_expired_events_cannot_override_a_current_snapshot():
    assert art_broadcast_override(None, (True, 100), now=101) is None
    assert art_broadcast_override(100, (True, 100), now=121) is None


def _published_device(device):
    device.entity_id = "media_player.example"
    device._power_off_in_progress = Mock(return_value=False)
    device._report_art_mode_as_off = True
    published = SimpleNamespace(
        state=MediaPlayerState.OFF, attributes={"art_mode_status": "on"}
    )
    device.hass.states = SimpleNamespace(get=lambda _: published)

    def publish():
        mode = device._art_mode_is_on()
        published.attributes["art_mode_status"] = "on" if mode else "off"
        published.state = device.state

    device.async_write_ha_state.side_effect = publish
    return published


def test_timestamp_only_shared_responses_do_not_republish_unchanged_state(device):
    _published_device(device)
    coordinator = device._get_ip_control_state_coordinator()
    device._attach_shared_ip_listener()
    for _ in range(100):
        coordinator.emit("Movie")
    assert device.async_write_ha_state.call_count == 1
    coordinator.emit("Ambient")
    assert device.async_write_ha_state.call_count == 2
    assert device.state == MediaPlayerState.OFF


def test_source_or_channel_changes_still_publish_from_the_shared_response(device):
    _published_device(device)
    coordinator = device._get_ip_control_state_coordinator()
    device._attach_shared_ip_listener()
    coordinator.data["tv"]["inputSource"] = "HDMI2"
    coordinator.emit("Movie")
    coordinator.data["channel"] = {"channelNum": "7"}
    coordinator.emit("Movie")
    assert device.async_write_ha_state.call_count == 3


def test_broadcast_grace_expiry_still_publishes_when_raw_fields_do_not_change(
    device, monkeypatch
):
    from custom_components.samsungtv_smart import media_player

    now = [time.monotonic()]
    monkeypatch.setattr(media_player, "time", SimpleNamespace(monotonic=lambda: now[0]))
    published = _published_device(device)
    coordinator = device._get_ip_control_state_coordinator()
    coordinator.data["polled_at"] = now[0]
    device.hass.data[DOMAIN][device._entry_id][DATA_ART_API].art_mode_last_broadcast = (
        True,
        now[0],
    )
    device._attach_shared_ip_listener()
    assert published.attributes["art_mode_status"] == "on"
    now[0] += 21
    coordinator.data["polled_at"] = now[0]
    device._shared_ip_updated()
    assert published.attributes["art_mode_status"] == "off"
    assert published.state == MediaPlayerState.ON
