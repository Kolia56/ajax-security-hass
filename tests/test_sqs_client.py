"""Tests for the SQS client's lazy aiobotocore detection.

On a fresh HACS install Home Assistant installs ``aiobotocore`` in the
background; ``sqs_client`` can be imported during that window, freezing
``HAS_AIOBOTOCORE`` False for the whole session. ``_ensure_aiobotocore``
re-attempts the import at client-init time so SQS self-recovers once the
dependency lands on disk, without a manual restart.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from custom_components.ajax import sqs_client


def test_ensure_aiobotocore_short_circuits_when_already_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqs_client, "HAS_AIOBOTOCORE", True)
    assert sqs_client._ensure_aiobotocore() is True


def test_ensure_aiobotocore_rebinds_after_deferred_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the dependency becoming importable after the frozen-False import."""
    # Frozen state: imported before aiobotocore finished installing.
    monkeypatch.setattr(sqs_client, "HAS_AIOBOTOCORE", False)
    monkeypatch.setattr(sqs_client, "get_session", None)

    sentinel_session = object()
    fake_session_mod = ModuleType("aiobotocore.session")
    fake_session_mod.get_session = lambda: sentinel_session  # type: ignore[attr-defined]
    fake_aiobotocore = ModuleType("aiobotocore")
    fake_aiobotocore.session = fake_session_mod  # type: ignore[attr-defined]

    fake_client_error = type("ClientError", (Exception,), {})
    fake_botocore_exc = ModuleType("botocore.exceptions")
    fake_botocore_exc.ClientError = fake_client_error  # type: ignore[attr-defined]
    fake_botocore = ModuleType("botocore")
    fake_botocore.exceptions = fake_botocore_exc  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "aiobotocore", fake_aiobotocore)
    monkeypatch.setitem(sys.modules, "aiobotocore.session", fake_session_mod)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", fake_botocore_exc)

    assert sqs_client._ensure_aiobotocore() is True
    assert sqs_client.HAS_AIOBOTOCORE is True
    assert sqs_client.get_session is fake_session_mod.get_session
    assert sqs_client.ClientError is fake_client_error


def test_client_init_raises_when_aiobotocore_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the dependency still unavailable, the client degrades by raising."""
    monkeypatch.setattr(sqs_client, "_ensure_aiobotocore", lambda: False)
    with pytest.raises(ImportError, match="aiobotocore required"):
        sqs_client.AjaxSQSClient(
            aws_access_key_id="k",
            aws_secret_access_key="s",
            queue_name="q",
        )


def test_client_init_succeeds_when_aiobotocore_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once available, the client builds and uses the resolved session factory."""
    sentinel_session = object()
    monkeypatch.setattr(sqs_client, "_ensure_aiobotocore", lambda: True)

    def _fake_get_session() -> Any:
        return sentinel_session

    monkeypatch.setattr(sqs_client, "get_session", _fake_get_session)

    client = sqs_client.AjaxSQSClient(
        aws_access_key_id="k",
        aws_secret_access_key="s",
        queue_name="q",
    )
    assert client._session is sentinel_session
