"""Contract tests against real Home Assistant, isolated from standalone stubs."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations, mock_bluetooth):
    """Load real HA contracts without starting the runner's USB/BLE hardware."""
    # HA skips the USB monitor inside Docker, so a container-only test pass
    # otherwise hides the missing pyudev dependency on GitHub's Linux runner.
    # Keep real dependency setup and flows; isolate only the hardware monitor.
    with patch(
        "homeassistant.components.usb.USBDiscovery._async_start_monitor",
        new_callable=AsyncMock,
    ):
        yield
