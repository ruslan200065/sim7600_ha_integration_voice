"""Configuration for pytest.

With pytest-homeassistant-custom-component installed every test runs. Without
it (e.g. on Windows where Home Assistant cannot be installed) only the pure
modem/codec tests are collected; the integration package is stubbed so its
submodules import without running the Home Assistant based __init__.py.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:
    HAS_HA = False
else:
    HAS_HA = True

PURE_TESTS = {
    "test_sms_codec.py",
    "test_modem_protocol.py",
    "test_modem.py",
    "test_gps.py",
    "test_sms.py",
}

if HAS_HA:
    if sys.platform == "win32":
        # Windows-only fixes; Linux CI keeps the strict behaviour.
        # 1. The HA plugin disables sockets, but Windows event loops need a
        #    socketpair for their self-pipe.
        # 2. aiodns needs a SelectorEventLoop on Windows. The HA plugin pins its
        #    loop policy, so the loop factory is swapped on that instance.
        import pytest_socket

        def _noop_disable_socket(*_args: object, **_kwargs: object) -> None:
            """Keep sockets enabled on Windows."""

        pytest_socket.disable_socket = _noop_disable_socket
        asyncio.get_event_loop_policy()._loop_factory = asyncio.SelectorEventLoop  # type: ignore[attr-defined]

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Enable custom integrations for all tests."""
        yield

else:
    _root = Path(__file__).resolve().parent.parent / "custom_components"
    for _name, _path in (
        ("custom_components", _root),
        ("custom_components.sim7600", _root / "sim7600"),
    ):
        if _name not in sys.modules:
            _module = types.ModuleType(_name)
            _module.__path__ = [str(_path)]
            sys.modules[_name] = _module

    collect_ignore = [
        path.name
        for path in Path(__file__).parent.glob("test_*.py")
        if path.name not in PURE_TESTS
    ]
