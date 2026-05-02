"""Translation file consistency tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = ROOT / "custom_components" / "ajax"


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return all scalar JSON paths from a nested translation object."""
    if isinstance(value, dict):
        paths: set[tuple[str, ...]] = set()
        for key, child in value.items():
            paths.update(_leaf_paths(child, (*prefix, key)))
        return paths
    return {prefix}


def test_translation_files_match_strings_schema() -> None:
    """Every shipped translation must expose the same leaf keys as strings.json."""
    strings = json.loads((INTEGRATION_DIR / "strings.json").read_text())
    expected_paths = _leaf_paths(strings)

    assert ("config", "step", "reconfigure", "data", "verify_ssl") in expected_paths
    assert ("config", "step", "reconfigure", "data_description", "verify_ssl") in expected_paths

    for translation_path in sorted((INTEGRATION_DIR / "translations").glob("*.json")):
        translation = json.loads(translation_path.read_text())
        assert _leaf_paths(translation) == expected_paths, translation_path.name
