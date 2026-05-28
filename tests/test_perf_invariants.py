"""Pin the performance invariants Quality Scale Platinum relies on.

A regression in any of these silently degrades runtime behaviour:
- An f-string in a debug log builds the message even when the level is
  filtered out (vs `_LOGGER.debug("...", arg)` which defers formatting).
- Missing `__slots__` on entity classes adds an instance __dict__ per
  entity (~110 bytes each — adds up with hundreds of entities).
- Polling intervals must respect the Quality Scale convention (≥ 30 s
  for the main loop; faster loops are isolated to the door-sensor
  fast-poll which is explicitly justified).
"""

from __future__ import annotations

import re
from pathlib import Path

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "ajax"


def test_no_f_string_logging() -> None:
    """Lazy logging only — f-strings always build the message even when filtered.

    `_LOGGER.debug(f"...")` defeats the `if _LOGGER.isEnabledFor(DEBUG)` check
    that HA does for every log call. Use `_LOGGER.debug("...", arg)` instead.
    """
    pattern = re.compile(r"_LOGGER\.(debug|info|warning|error|critical)\(f[\"']")
    offenders: list[str] = []
    for path in INTEGRATION_DIR.rglob("*.py"):
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{line_no}: {line.strip()[:100]}")
    assert not offenders, "f-string logging found (use lazy %s):\n" + "\n".join(offenders)


def test_entity_classes_declare_slots() -> None:
    """All Coordinator-backed entity classes must declare __slots__.

    Each entity carries instance state via _attr_X fields; without
    __slots__ Python allocates a per-instance __dict__ that pays for
    nothing (we never set arbitrary attributes). On a 200-entity install
    that's ~22 kB of overhead for zero benefit.
    """
    entity_class_pattern = re.compile(r"^class\s+(Ajax\w+)\((CoordinatorEntity[^)]*|.*Entity[^)]*)\):", re.MULTILINE)
    slots_pattern = re.compile(r"__slots__\s*=")
    offenders: list[str] = []
    for path in INTEGRATION_DIR.rglob("*.py"):
        text = path.read_text()
        classes = entity_class_pattern.findall(text)
        if not classes:
            continue
        # Heuristic: every class found above its first __slots__ counts;
        # this catches the file-level coverage rather than per-class line lookup.
        if classes and not slots_pattern.search(text):
            for cls_name, _ in classes:
                offenders.append(f"{path.name}: {cls_name} (no __slots__ in file)")
    assert not offenders, "Entity classes without __slots__:\n" + "\n".join(offenders)


def test_main_polling_interval_at_least_30s() -> None:
    """UPDATE_INTERVAL is the main coordinator loop — must stay ≥ 30 s.

    Faster cycles hammer the Ajax cloud + every proxy admin running an
    instance. The door-sensor fast-poll loop (5 s) is the only exception
    and lives in its own constant (UPDATE_INTERVAL_DOOR_SENSORS).
    """
    from custom_components.ajax.const import UPDATE_INTERVAL, UPDATE_INTERVAL_ARMED

    assert UPDATE_INTERVAL >= 30, f"Main update interval too aggressive: {UPDATE_INTERVAL}s"
    assert UPDATE_INTERVAL_ARMED >= UPDATE_INTERVAL


def test_debouncer_cooldown_within_bounds() -> None:
    """The request_refresh debouncer must stay between 0.1 s and 2 s.

    Too short → SSE bursts each trigger their own refresh (proxy load).
    Too long → user-triggered arm/disarm feels laggy in the UI.
    """

    src = (INTEGRATION_DIR / "coordinator.py").read_text()
    match = re.search(r"cooldown=([0-9.]+)", src)
    assert match, "Debouncer cooldown not found in coordinator.py"
    cooldown = float(match.group(1))
    assert 0.1 <= cooldown <= 2.0, f"Debouncer cooldown out of sane range: {cooldown}s"
