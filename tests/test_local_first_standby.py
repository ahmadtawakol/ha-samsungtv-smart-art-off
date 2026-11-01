from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.samsungtv_smart.api.ipcontrol import (
    SamsungIPControlAuthError,
    SamsungIPControlError,
)
from custom_components.samsungtv_smart.switch import SamsungTVPowerSwitch


def device(*, local=True, local_error=None, cloud=True, cloud_error=None):
    entity = object.__new__(SamsungTVPowerSwitch)
    entity._log = MagicMock()
    entity._device_id = "synthetic-device" if cloud else None
    entity._device_name = "Test TV"
    entity._set_optimistic = MagicMock()
    entity._get_media_player_entity_id = lambda: "media_player.test_tv"
    entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    calls = []

    async def power_off():
        calls.append("local")
        if local_error:
            raise local_error
        return "powerOff"

    async def cloud_off(*_args):
        calls.append("cloud")
        if cloud_error:
            raise cloud_error

    local_client = SimpleNamespace(async_power_off=AsyncMock(side_effect=power_off))
    cloud_client = SimpleNamespace(
        execute_device_command=AsyncMock(side_effect=cloud_off)
    )
    entity._get_ip_control = MagicMock(return_value=local_client if local else None)
    entity._get_st_client = AsyncMock(return_value=cloud_client)
    return entity, calls


async def test_local_success_needs_no_cloud_client_or_remote_fallback():
    entity, calls = device()
    await entity.async_turn_off()
    assert calls == ["local"]
    entity._get_st_client.assert_not_awaited()
    entity.hass.services.async_call.assert_not_awaited()
    entity._set_optimistic.assert_called_once_with(False)


@pytest.mark.parametrize(
    "error", [SamsungIPControlError("offline"), SamsungIPControlAuthError("rejected")]
)
async def test_local_failure_keeps_cloud_standby(error):
    entity, calls = device(local_error=error)
    await entity.async_turn_off()
    assert calls == ["local", "cloud"]
    entity.hass.services.async_call.assert_not_awaited()


async def test_unpaired_tv_still_uses_cloud():
    entity, calls = device(local=False)
    await entity.async_turn_off()
    assert calls == ["cloud"]


async def test_both_transports_failing_retain_the_existing_media_player_path():
    entity, calls = device(
        local_error=SamsungIPControlError("offline"),
        cloud_error=RuntimeError("offline"),
    )
    await entity.async_turn_off()
    assert calls == ["local", "cloud"]
    entity.hass.services.async_call.assert_awaited_once_with(
        "media_player",
        "turn_off",
        {"entity_id": "media_player.test_tv"},
        blocking=True,
    )
