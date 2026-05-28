"""Tests for AjaxSpaceSensor / AjaxDeviceSensor / AjaxVideoEdgeSensor.

We focus on the descriptor-driven plumbing: value_fn returns flow
through native_value, missing space/device returns None (HA = unknown),
buggy descriptors don't crash, and availability tracks both the
coordinator status and the per-device online flag.

We bypass CoordinatorEntity.__init__ (object.__new__) to avoid a full
HA fixture for what is essentially descriptor wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax.models import AjaxDevice, AjaxSpace, DeviceType, SecurityState
from custom_components.ajax.sensor import (
    AjaxDeviceSensor,
    AjaxSpaceSensor,
    AjaxSpaceSensorDescription,
)

# ---------------------------------------------------------------------------
# AjaxSpaceSensor
# ---------------------------------------------------------------------------


def _make_space_sensor(
    description: AjaxSpaceSensorDescription,
    *,
    space: AjaxSpace | None = None,
) -> AjaxSpaceSensor:
    sensor = object.__new__(AjaxSpaceSensor)
    sensor.entity_description = description
    sensor._space_id = "s1"
    sensor._entry = SimpleNamespace(entry_id="entry1")
    coordinator = SimpleNamespace(get_space=lambda sid: space)
    sensor.coordinator = coordinator
    return sensor


def test_space_sensor_native_value_calls_value_fn() -> None:
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.devices["d1"] = AjaxDevice(id="d1", name="X", type=DeviceType.MOTION_DETECTOR, space_id="s1", hub_id="hub1")
    desc = AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: len(s.devices))
    assert _make_space_sensor(desc, space=space).native_value == 1


def test_space_sensor_native_value_returns_none_when_space_missing() -> None:
    desc = AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: 5)
    assert _make_space_sensor(desc, space=None).native_value is None


def test_space_sensor_native_value_returns_none_when_value_fn_missing() -> None:
    """Descriptor without value_fn — must NOT crash, must return None."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    desc = AjaxSpaceSensorDescription(key="placeholder", value_fn=None)
    assert _make_space_sensor(desc, space=space).native_value is None


def test_space_sensor_extra_attributes_only_on_recent_events_key() -> None:
    """Only the `recent_events` sensor gets the recent-history attrs payload."""
    space = AjaxSpace(id="s1", name="Home", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.recent_events = []

    other = _make_space_sensor(AjaxSpaceSensorDescription(key="total_devices", value_fn=lambda s: 0), space=space)
    assert other.extra_state_attributes is None

    recent = _make_space_sensor(AjaxSpaceSensorDescription(key="recent_events", value_fn=lambda s: ""), space=space)
    assert recent.extra_state_attributes == {"events_count": 0}


def test_space_sensor_extra_attributes_handle_missing_space() -> None:
    """Recent-events sensor with no space returns None, not a crash."""
    sensor = _make_space_sensor(AjaxSpaceSensorDescription(key="recent_events", value_fn=lambda s: ""), space=None)
    assert sensor.extra_state_attributes is None


# ---------------------------------------------------------------------------
# AjaxDeviceSensor
# ---------------------------------------------------------------------------


def _device(online: bool = True) -> AjaxDevice:
    return AjaxDevice(
        id="d1",
        name="Sensor",
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
        online=online,
    )


def _make_device_sensor(
    sensor_desc: dict,
    *,
    device: AjaxDevice | None = None,
    update_success: bool = True,
) -> AjaxDeviceSensor:
    sensor = object.__new__(AjaxDeviceSensor)
    sensor._space_id = "s1"
    sensor._device_id = "d1"
    sensor._sensor_key = sensor_desc["key"]
    sensor._sensor_desc = sensor_desc
    space = SimpleNamespace(devices={"d1": device} if device else {})
    coordinator = SimpleNamespace(
        get_space=lambda sid: space,
        last_update_success=update_success,
    )
    sensor.coordinator = coordinator
    return sensor


def test_device_sensor_native_value_returns_value_fn() -> None:
    sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 85}, device=_device())
    assert sensor.native_value == 85


def test_device_sensor_native_value_returns_none_when_device_missing() -> None:
    sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 85}, device=None)
    assert sensor.native_value is None


def test_device_sensor_native_value_returns_none_when_value_fn_raises() -> None:
    """A buggy descriptor must NOT crash the platform."""

    def boom() -> int:
        raise ValueError("missing attribute")

    sensor = _make_device_sensor({"key": "battery", "value_fn": boom}, device=_device())
    assert sensor.native_value is None


def test_device_sensor_available_tracks_device_online_flag() -> None:
    """available=False the second a device goes offline."""
    online_sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 1}, device=_device(online=True))
    assert online_sensor.available is True

    offline_sensor = _make_device_sensor({"key": "battery", "value_fn": lambda: 1}, device=_device(online=False))
    assert offline_sensor.available is False


def test_device_sensor_available_false_when_coordinator_failed() -> None:
    sensor = _make_device_sensor(
        {"key": "battery", "value_fn": lambda: 1}, device=_device(online=True), update_success=False
    )
    assert sensor.available is False


def test_device_sensor_init_wires_descriptor_metadata() -> None:
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
    from homeassistant.const import UnitOfTemperature

    coord = MagicMock()
    sensor = AjaxDeviceSensor(
        coordinator=coord,
        space_id="s1",
        device_id="d1",
        sensor_key="temperature",
        sensor_desc={
            "key": "temperature",
            "device_class": SensorDeviceClass.TEMPERATURE,
            "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
            "state_class": SensorStateClass.MEASUREMENT,
            "translation_key": "temperature",
            "value_fn": lambda: 21.5,
        },
    )
    assert sensor._attr_unique_id == "d1_temperature"
    assert sensor._attr_device_class is SensorDeviceClass.TEMPERATURE
    assert sensor._attr_native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert sensor._attr_state_class is SensorStateClass.MEASUREMENT
    assert sensor._attr_translation_key == "temperature"
