"""Two #273 regressions: a latched running-app veto, and a lost recovery backoff.

1. art_mode_status latched off for 13.2 h on a Frame showing art. The
   running-app veto is the FIRST test in _art_mode_is_on, and _get_running_app()
   — the only writer that resets _running_app to DEFAULT_APP — runs exclusively
   under `state == MediaPlayerState.ON`. A Frame in Art Mode reports state OFF,
   so once it settles into art the value can never be refreshed: a brief power
   cycle that catches an app in the foreground latches the veto and pins
   art_mode_status off until the TV is genuinely switched on again. The veto is
   now gated on the TV being ON, where a visible foreground app is the only
   thing it was ever meant to catch.

2. Lifting the recovery cooldown on ms.channel.ready alone (8.8.15) removed all
   spacing between recovery cycles on a channel that never answers, taking the
   wedge count 5 -> 94. Unproductive cycles now double the cooldown, and ready
   only lifts it while none are outstanding.

Both modules need Home Assistant to import, so this is pinned on the source.
"""

from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1] / "custom_components" / "samsungtv_smart"
MEDIA_PLAYER = (ROOT / "media_player.py").read_text()
ART = (ROOT / "api" / "art.py").read_text()


def _block(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


class RunningAppVetoTest(unittest.TestCase):
    """The veto may only fire on a value that can still be refreshed."""

    def setUp(self):
        self.block = _block(
            MEDIA_PLAYER,
            "    def _art_mode_is_on",
            "    @property\n    def extra_state_attributes",
        )

    def test_the_veto_is_gated_on_the_tv_being_on(self):
        self.assertIn("self._state == MediaPlayerState.ON", self.block)
        gate = self.block.index("self._state == MediaPlayerState.ON")
        veto = self.block.index("self._running_app not in (")
        self.assertLess(gate, veto)

    def test_the_veto_still_precedes_the_panel_truth(self):
        # Order is unchanged for a TV that is ON: a visible app still wins over
        # a spurious art_mode_changed (the 2024-Frame fix this guard exists for).
        veto = self.block.index("self._running_app not in (")
        panel = self.block.index("shared = self._shared_panel_art_state()")
        self.assertLess(veto, panel)

    def test_an_off_tv_reaches_the_panel_truth(self):
        # Reproduce the branch: latched app + TV not ON must not short-circuit.
        def veto_fires(state_is_on: bool, running_app: str | None) -> bool:
            return state_is_on and running_app not in (None, "TV/HDMI")

        self.assertFalse(veto_fires(False, "Netflix"))  # the #273 latch
        self.assertTrue(veto_fires(True, "Netflix"))  # 2024-Frame fix intact
        self.assertFalse(veto_fires(True, "TV/HDMI"))
        self.assertFalse(veto_fires(True, None))


class RecoveryBackoffTest(unittest.TestCase):
    """A channel that never answers is retried further and further apart."""

    def test_unproductive_cycles_escalate_the_cooldown(self):
        block = _block(ART, "    def _note_request_timeout", "\n    async def ")
        self.assertIn("if self._got_response_since_connect:", block)
        self.assertIn("self._unproductive_cycles = 0", block)
        self.assertIn("self._unproductive_cycles + 1", block)
        # The exponent is now the larger of the never-answered cycles and the
        # recurring-wedge count, so a channel that answers some requests and
        # times out on the rest also escalates (#273 follow-up).
        self.assertIn(
            "doublings = max(self._unproductive_cycles, self._recurring_wedges)", block
        )
        self.assertIn("2**doublings", block)

    def test_ready_only_lifts_the_cooldown_when_nothing_is_outstanding(self):
        block = _block(ART, "    async def _connect_once(", "\n    async def close")
        ready = block.index("if event == MS_CHANNEL_READY_EVENT:")
        connect = block.index("elif event == MS_CHANNEL_CONNECT_EVENT:")
        ready_body = block[ready:connect]
        self.assertIn("if self._unproductive_cycles == 0:", ready_body)
        gate = ready_body.index("if self._unproductive_cycles == 0:")
        lift = ready_body.index("self._request_cooldown_until = 0.0")
        self.assertLess(gate, lift)

    def test_a_real_answer_clears_the_backoff(self):
        block = _block(ART, "    async def _wait_for_response", "\n    def ")
        self.assertIn("self._got_response_since_connect = True", block)
        self.assertIn("self._unproductive_cycles = 0", block)

    def test_the_doubling_is_capped(self):
        self.assertIn("ART_WS_RECOVERY_BACKOFF_MAX_DOUBLINGS = 3", ART)
        # 30s base, capped at 3 doublings -> 240s worst case.
        self.assertIn("ART_WS_RECOVERY_COOLDOWN = 30.0", ART)


if __name__ == "__main__":
    unittest.main()
