"""Contract tests against real Home Assistant, isolated from standalone stubs."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow HA's loader to find this custom component."""
