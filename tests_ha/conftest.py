"""Home Assistant runtime fixtures for HA Tools Email."""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Load custom_components from this repository with a recorder available."""
    yield
