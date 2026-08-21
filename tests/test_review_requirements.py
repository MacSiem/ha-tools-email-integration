"""Regression checks for the HACS credential-handling review."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = ROOT / "custom_components/ha_tools_email/__init__.py"
STORAGE_PATH = ROOT / "custom_components/ha_tools_email/storage.py"
BRAND_PATH = ROOT / "custom_components/ha_tools_email/brand"


def _load_storage_module():
    """Load storage.py with minimal Home Assistant stubs."""

    class FakeStore:
        instances = []

        def __init__(self, hass, version, key):
            self.data = deepcopy(getattr(hass, "stored_data", None))
            self.saved = []
            self.__class__.instances.append(self)

        async def async_load(self):
            return deepcopy(self.data)

        async def async_save(self, data):
            self.data = deepcopy(data)
            self.saved.append(deepcopy(data))

    homeassistant = types.ModuleType("homeassistant")
    homeassistant_core = types.ModuleType("homeassistant.core")
    homeassistant_helpers = types.ModuleType("homeassistant.helpers")
    homeassistant_storage = types.ModuleType("homeassistant.helpers.storage")
    homeassistant_core.HomeAssistant = object
    homeassistant_storage.Store = FakeStore

    package = types.ModuleType("custom_components.ha_tools_email")
    package.__path__ = [str(INIT_PATH.parent)]
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.core": homeassistant_core,
            "homeassistant.helpers": homeassistant_helpers,
            "homeassistant.helpers.storage": homeassistant_storage,
            "custom_components.ha_tools_email": package,
        }
    )

    const_spec = importlib.util.spec_from_file_location(
        "custom_components.ha_tools_email.const", INIT_PATH.parent / "const.py"
    )
    const_module = importlib.util.module_from_spec(const_spec)
    assert const_spec and const_spec.loader
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    storage_spec = importlib.util.spec_from_file_location(
        "custom_components.ha_tools_email.storage", STORAGE_PATH
    )
    storage_module = importlib.util.module_from_spec(storage_spec)
    assert storage_spec and storage_spec.loader
    sys.modules[storage_spec.name] = storage_module
    storage_spec.loader.exec_module(storage_module)
    return storage_module, FakeStore


class ReviewRequirementTests(unittest.TestCase):
    def test_legacy_smtp_json_is_saved_to_store_before_removal(self) -> None:
        storage_module, fake_store_class = _load_storage_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_path = Path(temp_dir) / "ha-tools" / "smtp-config.json"
            legacy_path.parent.mkdir()
            legacy_path.write_text(
                json.dumps(
                    {
                        "server": "smtp.example.com",
                        "port": 465,
                        "username": "user@example.com",
                        "password": "app-password",
                        "sender": "sender@example.com",
                        "encryption": "ssl",
                        "default_recipient": "to@example.com",
                        "unexpected": "must-not-migrate",
                    }
                ),
                encoding="utf-8",
            )

            class FakeConfig:
                @staticmethod
                def path(*parts):
                    return str(Path(temp_dir).joinpath(*parts))

            class FakeHass:
                config = FakeConfig()
                stored_data = None

                @staticmethod
                async def async_add_executor_job(func, *args):
                    return func(*args)

            email_storage = storage_module.EmailStorage(FakeHass())
            self.assertTrue(hasattr(email_storage, "async_get_smtp_config"))
            config = asyncio.run(email_storage.async_get_smtp_config())
            store = fake_store_class.instances[-1]

            self.assertEqual(config["password"], "app-password")
            self.assertNotIn("unexpected", config)
            self.assertEqual(store.saved[-1]["smtp_config"], config)
            self.assertFalse(legacy_path.exists())

    def test_smtp_credentials_use_home_assistant_store(self) -> None:
        init_source = INIT_PATH.read_text(encoding="utf-8")
        storage_source = STORAGE_PATH.read_text(encoding="utf-8")

        self.assertIn('"smtp_config":', storage_source)
        self.assertIn("async_get_smtp_config", storage_source)
        self.assertIn("async_save_smtp_config", storage_source)
        self.assertNotIn("json.dump(data", init_source)

    def test_sensitive_legacy_services_are_admin_only_and_do_not_mask_leak(self) -> None:
        source = INIT_PATH.read_text(encoding="utf-8")

        self.assertIn("from homeassistant.helpers.service import async_register_admin_service", source)
        for service in ("save_config", "get_config", "list_secrets"):
            self.assertIn(f'DOMAIN, "{service}"', source)
        self.assertNotIn("def _mask_password", source)
        self.assertNotIn('safe["available_secrets"]', source)

    def test_declared_floor_and_brand_assets_match_review(self) -> None:
        hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

        self.assertEqual(hacs["homeassistant"], "2024.7.0")
        self.assertFalse((BRAND_PATH / "logo.png").exists())
        self.assertFalse((BRAND_PATH / "logo@2x.png").exists())


if __name__ == "__main__":
    unittest.main()
