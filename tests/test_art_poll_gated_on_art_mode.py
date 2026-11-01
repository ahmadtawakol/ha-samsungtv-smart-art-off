"""Art-only reads must not be polled while the TV is not showing art (#273).

A Frame sitting on an input (HDMI) answers get_artmode_status but stays silent
on get_current_artwork and on the slideshow reads. The Frame Art coordinator
polls every 5 s, so each cycle piled up two requests that could only time out
after 5 s each. Three consecutive timeouts trip the art channel's wedge breaker
(ART_WS_TIMEOUT_TRIP), which force-closes a socket that is demonstrably
answering — 14 reconnects in 70 min measured on a 13-TV fleet, none of which
changed anything, because nothing was broken. The recovery backoff added in
8.9.6 only spaced the pointless reconnects out; it did not stop them.

Neither the current artwork nor the slideshow state can change while art mode
is off, so those reads are skipped and the already-published values are kept.
Art Mode follows the shared local reading when available. Otherwise, a slow
status fallback detects missed changes without repeating the artwork requests.

sensor.py needs Home Assistant to import, so the coordinator's update method is
exec'd from source against a fake art API that records every request.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1] / "custom_components" / "samsungtv_smart"
SENSOR = (ROOT / "sensor.py").read_text()


class _UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


CLOCK = _Clock()


def _policy_namespace(clock):
    """Load the policy in isolation from Home Assistant."""
    namespace = {"time": clock, "ConfigEntry": object}
    exec(
        compile((ROOT / "art_mode_state.py").read_text(), "art_mode_state.py", "exec"),
        namespace,
    )
    needed = {
        "CONF_IP_CONTROL_TOKEN",
        "CONF_ENABLE_IP_CONTROL",
        "DOMAIN",
        "DATA_IP_CONTROL_STATE_COORDINATOR",
        "CONF_IP_CONTROL_POLL_INTERVAL",
        "DEFAULT_IP_CONTROL_POLL_INTERVAL",
        "ART_MODE_STATUS_RECHECK_INTERVAL",
    }
    for source in (SENSOR, (ROOT / "const.py").read_text()):
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in needed:
                        namespace[target.id] = ast.literal_eval(node.value)
    helpers = [
        node
        for node in ast.parse(SENSOR).body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_ip_control_active", "_ip_control_poll_interval")
    ]
    exec(
        compile(ast.Module(body=helpers, type_ignores=[]), "sensor.py:policy", "exec"),
        namespace,
    )
    return namespace


def _load_update_method():
    start = SENSOR.index("    async def _async_update_data")
    end = SENSOR.index("\n    def _is_tv_powered_off", start)
    source = "class _Holder:\n" + SENSOR[start:end] + "\n"
    namespace = _policy_namespace(CLOCK)
    namespace.update(
        {
            "asyncio": asyncio,
            "UpdateFailed": _UpdateFailed,
            "CONF_SLIDESHOW_API": "slideshow_api",
            "Any": object,
        }
    )
    exec(compile(source, "sensor.py:_async_update_data", "exec"), namespace)
    return namespace["_Holder"]


_Holder = _load_update_method()
_update = _Holder._async_update_data
_should_refresh = _Holder._should_refresh_art_status
_fresh_panel = _Holder._shared_panel_art_status


class _Log:
    def debug(self, *args):
        pass

    info = warning = error = debug


class _Entry:
    entry_id = "abc"
    data: dict = {"slideshow_api": "slideshow"}
    options: dict = {}


class _ArtApi:
    """Records every art request; answers only get_artmode, like a Frame on HDMI."""

    def __init__(self, art_mode="off"):
        self.calls: list[str] = []
        self._art_mode = art_mode

    async def get_artmode(self):
        self.calls.append("get_artmode")
        return self._art_mode

    async def get_current(self):
        self.calls.append("get_current_artwork")
        # The real TV never answers this on an input: it times out.
        raise asyncio.TimeoutError

    async def get_slideshow_status(self):
        self.calls.append("get_slideshow_status")
        raise asyncio.TimeoutError

    async def get_auto_rotation_status(self):
        self.calls.append("get_auto_rotation_status")
        raise asyncio.TimeoutError

    async def detect_slideshow_api(self):
        self.calls.append("detect_slideshow_api")
        raise asyncio.TimeoutError

    async def available(self, *args, **kwargs):
        self.calls.append("available")
        raise asyncio.TimeoutError


class _Coordinator:
    """The fake self the extracted method runs against."""

    _should_refresh_art_status = _should_refresh
    _shared_panel_art_status = _fresh_panel

    def __init__(self, art_mode="off", previous=None):
        self._log = _Log()
        self._entry = _Entry()
        self._art_api = _ArtApi(art_mode)
        self._media_player_art_mode = art_mode
        self.data = previous
        self._backoff_until = None
        self._art_status_last_attempt = None
        self._connection_failures = 0
        self._max_connection_failures = 5
        self._thumbnail_failures = 0
        self._thumbnail_backoff_until = None
        self._thumbnail_fetch_enabled = True
        self._last_content_id = None
        self._store_retry_content_id = None
        self._store_retry_at = None
        self.background_tasks: list[str] = []

    # --- collaborators the method calls on self ---
    def _is_tv_powered_off(self):
        return False

    def _get_media_player_art_mode(self):
        return self._media_player_art_mode

    def _has_current_thumbnail(self):
        return True

    def _has_thumbnail_for(self, content_id):
        return False

    def _confirm_content_id(self, raw):
        return raw

    @property
    def _hass(self):
        return getattr(self, "_hass_override", self)

    def async_create_background_task(self, coro, name):
        coro.close()
        self.background_tasks.append(name)


def _run(coordinator):
    return asyncio.run(_update(coordinator))


class ArtModeOffTest(unittest.TestCase):
    """The case that produced the loop: the TV is on, showing an input."""

    def setUp(self):
        self.previous = {
            "art_mode": "on",
            "current_artwork": {"content_id": "MY_F0042"},
            "artwork_count": 17,
            "slideshow_status": "off",
            "api_version": None,
            "current_thumbnail_url": None,
            "tv_powered_off": False,
        }
        self.coordinator = _Coordinator("off", previous=self.previous)
        self.data = _run(self.coordinator)

    def test_no_art_only_request_is_sent(self):
        for request in (
            "get_current_artwork",
            "get_slideshow_status",
            "get_auto_rotation_status",
            "available",
        ):
            self.assertNotIn(request, self.coordinator._art_api.calls)

    def test_the_cycle_sends_nothing_at_all_when_the_media_player_knows(self):
        # art_mode comes from the media_player here, so a gated cycle must be
        # free: this is what stops the 5 s poll feeding the wedge breaker.
        self.assertEqual(self.coordinator._art_api.calls, [])

    def test_the_already_published_values_are_kept(self):
        self.assertEqual(self.data["current_artwork"], self.previous["current_artwork"])
        self.assertEqual(self.data["artwork_count"], 17)
        self.assertEqual(self.data["slideshow_status"], "off")

    def test_no_thumbnail_fetch_is_triggered(self):
        self.assertEqual(self.coordinator.background_tasks, [])

    def test_the_slideshow_api_is_not_detected_on_a_silent_channel(self):
        # Detection on a TV that cannot answer either endpoint would latch a
        # wrong choice into entry.data, as the capability probes once did.
        coordinator = _Coordinator("off", previous=self.previous)
        coordinator._entry = type("E", (_Entry,), {"data": {}})()
        _run(coordinator)
        self.assertNotIn("detect_slideshow_api", coordinator._art_api.calls)


class ArtModeOnTest(unittest.TestCase):
    """Art mode on: every read still happens."""

    def setUp(self):
        self.coordinator = _Coordinator("on")
        _run(self.coordinator)

    def test_the_current_artwork_is_still_read(self):
        self.assertIn("get_current_artwork", self.coordinator._art_api.calls)


class ArtModeRefreshTest(unittest.TestCase):
    def test_an_on_attribute_still_reads_status_from_the_tv(self):
        coordinator = _Coordinator("off")
        coordinator._media_player_art_mode = "on"

        _run(coordinator)

        self.assertIn("get_artmode", coordinator._art_api.calls)

    def test_the_media_players_canonical_verdict_is_preserved(self):
        coordinator = _Coordinator("off")
        coordinator._media_player_art_mode = "on"

        data = _run(coordinator)

        self.assertEqual(data["art_mode"], "on")

    def test_a_failed_status_read_does_not_invent_a_mode_change(self):
        coordinator = _Coordinator("on")
        coordinator._art_api.get_artmode = AsyncMock(side_effect=asyncio.TimeoutError)

        data = _run(coordinator)

        coordinator._art_api.get_artmode.assert_awaited_once_with()
        self.assertEqual(data["art_mode"], "on")


class ArtStatusPollingPolicyTest(unittest.TestCase):
    def setUp(self):
        CLOCK.now = 1000.0
        self.coordinator = _Coordinator("on")

    def tearDown(self):
        CLOCK.now = 1000.0

    def paired_snapshot(self):
        self.coordinator._entry = SimpleNamespace(
            entry_id="abc",
            data={"ip_control_token": "synthetic"},
            options={"enable_ip_control": True, "ip_control_poll_interval": 10},
        )
        snapshot = {
            "powered_off": False,
            "tv": {"pictureMode": "Ambient"},
            "polled_at": CLOCK.now,
        }
        local = SimpleNamespace(data=snapshot, last_update_success=True)
        self.coordinator._hass_override = SimpleNamespace(
            data={"samsungtv_smart": {"abc": {"ip_control_state_coordinator": local}}}
        )
        return snapshot, local

    def test_a_day_of_fresh_ip_readings_adds_no_art_status_queries(self):
        snapshot, _ = self.paired_snapshot()
        requests = 0
        for _ in range(17_280):
            snapshot["polled_at"] = CLOCK.now
            requests += self.coordinator._should_refresh_art_status("on")
            CLOCK.advance(5)
        self.assertEqual(requests, 0)

    def test_a_day_without_ip_uses_only_the_five_minute_backstop(self):
        requests = 0
        for _ in range(17_280):
            requests += self.coordinator._should_refresh_art_status("on")
            CLOCK.advance(5)
        self.assertEqual(requests, 288)

    def test_shared_reply_updates_a_stale_on_value_without_querying_the_art_api(self):
        snapshot, _ = self.paired_snapshot()
        snapshot["tv"]["pictureMode"] = "Movie"
        self.coordinator._art_api.art_mode = True
        result = _run(self.coordinator)
        self.assertEqual(result["art_mode"], "off")
        self.assertNotIn("get_artmode", self.coordinator._art_api.calls)

    def test_shared_reply_is_used_even_without_a_published_media_player_status(self):
        self.paired_snapshot()
        self.coordinator._media_player_art_mode = None
        self.coordinator._art_api.art_mode = False
        result = _run(self.coordinator)
        self.assertEqual(result["art_mode"], "on")
        self.assertNotIn("get_artmode", self.coordinator._art_api.calls)

    def test_a_fresh_shared_reply_is_not_blocked_by_a_stale_off_presentation(self):
        self.paired_snapshot()
        self.coordinator._media_player_art_mode = "off"
        self.coordinator._is_tv_powered_off = lambda: True
        result = _run(self.coordinator)
        self.assertEqual(result["art_mode"], "on")
        self.assertFalse(result["tv_powered_off"])
        self.assertNotIn("get_artmode", self.coordinator._art_api.calls)

    def test_recent_transition_still_outranks_a_trailing_panel_read(self):
        snapshot, _ = self.paired_snapshot()
        snapshot["polled_at"] = CLOCK.now - 5
        snapshot["tv"]["pictureMode"] = "Movie"
        self.coordinator._art_api.art_mode_last_broadcast = (True, CLOCK.now - 1)
        self.assertEqual(_run(self.coordinator)["art_mode"], "on")

    def test_recent_mode_notification_defers_a_fallback_check(self):
        self.coordinator._art_api.art_mode_last_broadcast = (True, CLOCK.now)
        self.assertFalse(self.coordinator._should_refresh_art_status("on"))
        CLOCK.advance(299)
        self.assertFalse(self.coordinator._should_refresh_art_status("on"))
        CLOCK.advance(1)
        self.assertTrue(self.coordinator._should_refresh_art_status("on"))

    def test_failed_request_is_not_repeated_at_the_artwork_poll_rate(self):
        read = AsyncMock(side_effect=asyncio.TimeoutError)
        self.coordinator._art_api.get_artmode = read
        _run(self.coordinator)
        CLOCK.advance(5)
        data = _run(self.coordinator)
        read.assert_awaited_once_with()
        self.assertEqual(data["art_mode"], "on")

    def test_stale_ip_snapshot_allows_one_fallback_then_waits(self):
        snapshot, _ = self.paired_snapshot()
        CLOCK.advance(31)
        self.assertTrue(self.coordinator._should_refresh_art_status("on"))
        CLOCK.advance(5)
        self.assertFalse(self.coordinator._should_refresh_art_status("on"))
        CLOCK.advance(300)
        snapshot["polled_at"] = CLOCK.now
        self.assertFalse(self.coordinator._should_refresh_art_status("on"))

    def test_disabled_ip_control_cannot_suppress_fallback(self):
        self.paired_snapshot()
        self.coordinator._entry.options["enable_ip_control"] = False
        self.assertTrue(self.coordinator._should_refresh_art_status("on"))

    def test_failed_or_unusable_snapshots_do_not_count_as_fresh(self):
        cases = (
            {"last_update_success": False},
            {"powered_off": True},
            {"polled_at": None},
            {"polled_at": "invalid"},
            {"polled_at": CLOCK.now + 100},
            {"tv": {}},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.coordinator._art_status_last_attempt = None
                snapshot, local = self.paired_snapshot()
                if "last_update_success" in changes:
                    local.last_update_success = changes["last_update_success"]
                else:
                    snapshot.update(changes)
                self.assertTrue(self.coordinator._should_refresh_art_status("on"))

    def test_ip_freshness_respects_the_configured_poll_interval(self):
        self.paired_snapshot()
        self.coordinator._entry.options["ip_control_poll_interval"] = 60
        CLOCK.advance(100)
        self.assertFalse(self.coordinator._should_refresh_art_status("on"))

    def test_unknown_status_is_checked_once_and_then_rate_limited(self):
        self.assertTrue(self.coordinator._should_refresh_art_status(None))
        CLOCK.advance(5)
        self.assertFalse(self.coordinator._should_refresh_art_status(None))

    def test_standalone_art_sensor_keeps_its_cached_value_between_checks(self):
        self.coordinator._media_player_art_mode = None
        self.coordinator._art_api.art_mode = True
        first = _run(self.coordinator)
        CLOCK.advance(5)
        second = _run(self.coordinator)
        self.assertEqual(first["art_mode"], "on")
        self.assertEqual(second["art_mode"], "on")
        self.assertEqual(self.coordinator._art_api.calls.count("get_artmode"), 1)

    def test_known_off_never_adds_an_art_status_query(self):
        self.assertFalse(self.coordinator._should_refresh_art_status("off"))
        CLOCK.advance(86400)
        self.assertFalse(self.coordinator._should_refresh_art_status("off"))


class ArtModeUnknownTest(unittest.TestCase):
    """An unknown reading must not gate: only a positive "off" skips."""

    def setUp(self):
        self.coordinator = _Coordinator("on")
        # media_player state not up yet -> the direct API fallback is used.
        self.coordinator._media_player_art_mode = None
        self.coordinator._art_api._art_mode = None
        _run(self.coordinator)

    def test_the_direct_api_is_asked(self):
        self.assertIn("get_artmode", self.coordinator._art_api.calls)

    def test_the_current_artwork_is_still_read(self):
        self.assertIn("get_current_artwork", self.coordinator._art_api.calls)


class SourceShapeTest(unittest.TestCase):
    """Guard the two properties the behavioural tests rely on."""

    def test_the_gate_is_a_positive_off_not_a_falsy_check(self):
        self.assertIn('art_mode_off = data["art_mode"] == "off"', SENSOR)

    def test_get_artmode_is_read_before_the_gate(self):
        poll = SENSOR.index("art_mode = await self._art_api.get_artmode()")
        gate = SENSOR.index('art_mode_off = data["art_mode"] == "off"')
        self.assertLess(poll, gate)


if __name__ == "__main__":
    unittest.main()
