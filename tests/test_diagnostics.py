"""Unit tests for the zero-IO diagnostics snapshots.

The heavy `ajax_data` dump (REST round-trips) belongs in an HA
integration test; here we pin the cheap snapshots that drive triage:
runtime state, connectivity status, stats counters, cache sizes and
spaces summary.
"""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax import diagnostics


def _fake_coordinator(**overrides) -> SimpleNamespace:
    """Build a coordinator stub with just the attributes diagnostics reads."""
    api = SimpleNamespace(
        _devices_cache={("hub1", True): (time.time(), [])},
        _devices_cache_ttl=5.0,
        _space_cache={"sp1": (time.time(), {})},
        _space_cache_ttl=5.0,
    )
    account = SimpleNamespace(
        spaces={
            "sp1": SimpleNamespace(
                security_state=SimpleNamespace(value="disarmed"),
                group_mode_enabled=True,
                devices={"d1": {}, "d2": {}},
                video_edges={"ve1": {}},
                smart_locks={},
                groups={"g1": {}},
                recent_events=[{}, {}, {}],
            ),
        }
    )
    config_entry = SimpleNamespace(data={"auth_mode": "proxy_secure"})
    coord = SimpleNamespace(
        api=api,
        account=account,
        config_entry=config_entry,
        last_update_success=True,
        last_exception=None,
        update_interval=timedelta(seconds=30),
        _cycle_counter=12,
        _consecutive_auth_errors=0,
        _last_metadata_refresh=time.time() - 100,
        stats={
            "events_sse_received": 17,
            "events_sqs_received": 0,
            "events_onvif_received": 4,
            "auth_errors": 0,
            "discovery_refreshes": 1,
        },
        sse_manager=None,
        sqs_manager=None,
        onvif_manager=None,
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def test_integration_version_matches_manifest() -> None:
    # The function caches the value, so this also verifies the cache returns
    # the same string on a second call.
    first = diagnostics._integration_version()
    second = diagnostics._integration_version()
    assert first == second
    assert first != "unknown", "manifest.json must be readable from tests"


def test_seconds_since_returns_none_for_falsy() -> None:
    assert diagnostics._seconds_since(None) is None
    assert diagnostics._seconds_since(0) is None


def test_seconds_since_rounds_to_tenth() -> None:
    now = time.time()
    elapsed = diagnostics._seconds_since(now - 12.34)
    # Rounded to 1 decimal place per the function's contract.
    assert elapsed is not None
    assert round(elapsed - 12.3, 1) <= 0.1


def test_runtime_snapshot_shape_and_values() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._runtime_snapshot(coord)
    assert snap["auth_mode"] == "proxy_secure"
    assert snap["last_update_success"] is True
    assert snap["last_exception"] is None
    assert snap["update_interval_seconds"] == 30
    assert snap["cycle_counter"] == 12
    assert snap["consecutive_auth_errors"] == 0
    assert snap["spaces"] == 1
    assert snap["integration_version"] != "unknown"
    # 100 s ± rounding noise.
    assert snap["seconds_since_last_metadata_refresh"] >= 99


def test_runtime_snapshot_formats_exception_when_set() -> None:
    coord = _fake_coordinator(last_exception=RuntimeError("boom"))
    snap = diagnostics._runtime_snapshot(coord)
    assert "boom" in snap["last_exception"]


def test_runtime_snapshot_handles_missing_account() -> None:
    coord = _fake_coordinator(account=None)
    snap = diagnostics._runtime_snapshot(coord)
    assert snap["spaces"] == 0


def test_connectivity_snapshot_all_disabled() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap == {
        "sse": {"enabled": False, "connected": False},
        "sqs": {"enabled": False, "connected": False, "seconds_since_last_event": None},
        "onvif": {"configured_count": 0, "connected_count": 0},
    }


def test_connectivity_snapshot_with_sse_connected() -> None:
    sse_client = MagicMock()
    sse_client.is_connected = MagicMock(return_value=True)
    sse_manager = SimpleNamespace(sse_client=sse_client)
    coord = _fake_coordinator(sse_manager=sse_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["sse"] == {"enabled": True, "connected": True}


def test_connectivity_snapshot_with_sqs_last_event() -> None:
    sqs_client = MagicMock()
    sqs_client.is_connected = MagicMock(return_value=True)
    last = time.time() - 4
    sqs_manager = SimpleNamespace(sqs_client=sqs_client, _last_event_time=last)
    coord = _fake_coordinator(sqs_manager=sqs_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["sqs"]["enabled"] is True
    assert snap["sqs"]["connected"] is True
    assert snap["sqs"]["seconds_since_last_event"] >= 3.9


def test_connectivity_snapshot_with_onvif() -> None:
    onvif_manager = SimpleNamespace(_clients={"a": object(), "b": object()}, connected_count=1)
    coord = _fake_coordinator(onvif_manager=onvif_manager)
    snap = diagnostics._connectivity_snapshot(coord)
    assert snap["onvif"] == {"configured_count": 2, "connected_count": 1}


def test_cache_snapshot_reports_sizes_and_ttls() -> None:
    coord = _fake_coordinator()
    snap = diagnostics._cache_snapshot(coord)
    assert snap == {
        "devices_cache_entries": 1,
        "devices_cache_ttl_seconds": 5.0,
        "space_cache_entries": 1,
        "space_cache_ttl_seconds": 5.0,
    }


def test_cache_snapshot_handles_missing_attributes() -> None:
    """An older API instance without cache attributes must not crash."""
    coord = _fake_coordinator(api=SimpleNamespace())
    snap = diagnostics._cache_snapshot(coord)
    assert snap["devices_cache_entries"] == 0
    assert snap["space_cache_entries"] == 0


def test_spaces_summary_counts_per_space() -> None:
    coord = _fake_coordinator()
    summary = diagnostics._spaces_summary(coord)
    assert summary == [
        {
            "security_state": "disarmed",
            "group_mode_enabled": True,
            "devices": 2,
            "video_edges": 1,
            "smart_locks": 0,
            "groups": 1,
            "recent_events": 3,
        }
    ]


def test_spaces_summary_empty_when_no_account() -> None:
    coord = _fake_coordinator(account=None)
    assert diagnostics._spaces_summary(coord) == []


def test_runtime_diagnostics_bundles_all_sections() -> None:
    """The orchestrator must return exactly the documented sections."""
    coord = _fake_coordinator()
    bundle = diagnostics._runtime_diagnostics(coord)
    assert set(bundle.keys()) == {"runtime", "connectivity", "stats", "cache", "spaces"}
    # stats must be a *copy* (mutating downstream must not pollute the
    # coordinator's live counters).
    bundle["stats"]["events_sse_received"] = 9999
    assert coord.stats["events_sse_received"] == 17
