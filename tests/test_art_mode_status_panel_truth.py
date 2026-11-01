"""art_mode_status must follow the panel, not freeze on a stale WS reading (#248).

Measured over 7 days on an 11-Frame fleet: with the art-mode option off (the
recommended default), the transition INTO art mode left art_mode_status frozen
at its pre-transition value for hours — the panel showed art, getTVStates
reported pictureMode 'Ambient', but the attribute stayed 'off' because
_art_mode_is_on() only consulted the WebSocket art channel, which never
received the change event.

_art_mode_is_on reads fresh shared getTVStates.pictureMode (tri-state) before
the cached IP flag and WS/SmartThings fallbacks. media_player.py is
not importable without Home Assistant, so this is checked structurally and the
tri-state reader is checked by extraction.
"""

from pathlib import Path
import unittest

MEDIA_PLAYER = (
    Path(__file__).parents[1]
    / "custom_components"
    / "samsungtv_smart"
    / "media_player.py"
).read_text()


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


class TriStateReaderTest(unittest.TestCase):
    """_ip_control_panel_art_cached: True / False / None, cached only."""

    def setUp(self):
        self.block = _block(
            MEDIA_PLAYER,
            "    def _ip_control_panel_art_cached",
            "    def _ip_control_ambient_mode_active",
        )

    def test_it_reads_the_cached_coordinator_snapshot_not_a_live_call(self):
        self.assertIn(
            "coordinator = self._get_ip_control_state_coordinator()", self.block
        )
        self.assertIn('data.get("powered_off")', self.block)
        # Must not issue a request — it runs from a sync property.
        self.assertNotIn("await", self.block)
        self.assertNotIn("async_panel_shows_art", self.block)

    def test_unreadable_states_return_none_not_false(self):
        # No coordinator / no snapshot / powered off / no pictureMode -> None,
        # so _art_mode_is_on falls through instead of asserting "off".
        head = self.block[: self.block.index('return mode == "Ambient"')]
        self.assertEqual(head.count("return None"), 3)

    def test_ambient_is_the_only_true(self):
        self.assertIn('return mode == "Ambient"', self.block)

    def test_the_bool_helper_delegates_to_the_tristate(self):
        helper = _block(
            MEDIA_PLAYER,
            "    def _ip_control_ambient_mode_active",
            "    def _get_ip_control_input_source",
        )
        self.assertIn("return self._ip_control_panel_art_cached() is True", helper)
        # The coordinator-reading logic now lives in one place only.
        self.assertNotIn("coordinator", helper)


class ArtModeIsOnTest(unittest.TestCase):
    """The panel truth is consulted in the right order."""

    def setUp(self):
        self.block = _block(
            MEDIA_PLAYER,
            "    def _art_mode_is_on",
            "    @property\n    def extra_state_attributes",
        )

    def test_the_shared_panel_precedes_the_cached_ip_flag(self):
        panel = self.block.index("shared = self._shared_panel_art_state()")
        flag = self.block.index("if self._ip_art_mode is not None:")
        self.assertLess(panel, flag)

    def test_the_panel_is_consulted_before_the_websocket_and_smartthings(self):
        panel = self.block.index("shared = self._shared_panel_art_state()")
        art_api = self.block.index("art_api.art_mode")
        ws = self.block.index("self._ws.artmode_status")
        self.assertLess(panel, art_api)
        self.assertLess(panel, ws)

    def test_only_a_readable_panel_short_circuits(self):
        start = self.block.index("shared = self._shared_panel_art_state()")
        end = self.block.index("if self._ip_art_mode is not None:", start)
        head = self.block[start:end]
        self.assertIn("if shared is not None:", head)
        self.assertIn("return mode if override is None else override", head)

    def test_the_panel_is_only_trusted_on_art_capable_tvs(self):
        helper = _block(
            MEDIA_PLAYER,
            "    def _shared_panel_art_state",
            "    def _apply_shared_panel_art_state",
        )
        self.assertIn("self.support_art_mode == ArtModeSupport.UNSUPPORTED", helper)
        self.assertIn("return None", helper)

    def test_the_running_app_guard_still_leads(self):
        guard = self.block.index("self._running_app not in (")
        panel = self.block.index("shared = self._shared_panel_art_state()")
        self.assertLess(guard, panel)


if __name__ == "__main__":
    unittest.main()
