"""Coordinator entities that skip unchanged state updates."""

from copy import deepcopy

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity


class ChangedCoordinatorEntity(CoordinatorEntity):
    _published_coordinator_value: tuple | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.hass is None or self.entity_id is None:
            super()._handle_coordinator_update()
            return
        value = self.native_value
        visible = (
            self.available,
            (type(value), value),
            self.extra_state_attributes,
            self.icon,
            self.entity_picture,
            self.name,
            getattr(self, "native_unit_of_measurement", None),
        )
        if self.force_update or visible != self._published_coordinator_value:
            self._published_coordinator_value = deepcopy(visible)
            super()._handle_coordinator_update()
