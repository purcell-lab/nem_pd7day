"""
Pytest configuration.

Provides autouse fixtures that prevent test ordering contamination.
"""
import importlib
import pytest


@pytest.fixture(autouse=True)
def restore_sensor_module_globals():
    """
    Restore any module-level names in sensor.py that tests may have patched.

    Some tests patch `custom_components.nem_pd7day.sensor._amber_express_cutoff`
    using `unittest.mock.patch`. If a test fails mid-context-manager the mock can
    leak into subsequent tests.  This fixture captures the original reference
    before each test and restores it afterwards.
    """
    import custom_components.nem_pd7day.sensor as sensor_mod
    original = sensor_mod._amber_express_cutoff
    yield
    sensor_mod._amber_express_cutoff = original
