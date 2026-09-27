import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import sync_codex_model_catalog as catalog


class FakeResponse:
    """Minimal stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def fake_urlopen(payload: object):
    def opener(request, timeout=None):
        return FakeResponse(payload)

    return opener


CONFIG_TEMPLATE = """model_provider = "custom"
model = "alpha"

[model_providers.custom]
name = "Custom"
base_url = "{base_url}"
experimental_bearer_token = "secret-token"
"""


class FetchProviderModelsTests(unittest.TestCase):
    def test_parses_ids_and_keeps_first_seen_order(self) -> None:
        payload = {
            "data": [
                {"id": "beta"},
                {"id": "alpha"},
                {"id": "beta"},
                {"model": "gamma"},
                "delta",
            ]
        }
        with patch.object(
            catalog.urllib.request, "urlopen", fake_urlopen(payload)
        ):
            models = catalog.fetch_provider_models("http://127.0.0.1:1/v1", None, 1.0)
        self.assertEqual(models, ["beta", "alpha", "gamma", "delta"])

    def test_appends_models_to_the_base_url(self) -> None:
        self.assertEqual(
            catalog.models_endpoint("http://127.0.0.1:1/v1/"),
            "http://127.0.0.1:1/v1/models",
        )

    def test_reports_unreachable_provider(self) -> None:
        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with patch.object(catalog.urllib.request, "urlopen", boom):
            with self.assertRaises(catalog.CatalogError):
                catalog.fetch_provider_models("http://127.0.0.1:1/v1", None, 1.0)

    def test_reports_non_json_payload(self) -> None:
        class NotJson(FakeResponse):
            def __init__(self) -> None:
                self._body = b"<html>nope</html>"

        with patch.object(
            catalog.urllib.request, "urlopen", lambda request, timeout=None: NotJson()
        ):
            with self.assertRaises(catalog.CatalogError):
                catalog.fetch_provider_models("http://127.0.0.1:1/v1", None, 1.0)


class BuildCatalogTests(unittest.TestCase):
    def test_reuses_known_metadata_and_synthesizes_unknown_models(self) -> None:
        known = {
            "alpha": {
                "slug": "alpha",
                "display_name": "Alpha",
                "context_window": 123456,
                "priority": 5,
            }
        }
        built = catalog.build_catalog(["alpha", "beta"], known)
        by_slug = {entry["slug"]: entry for entry in built["models"]}

        self.assertEqual(by_slug["alpha"]["context_window"], 123456)
        self.assertEqual(by_slug["alpha"]["display_name"], "Alpha")
        self.assertEqual(by_slug["beta"]["display_name"], "beta")
        self.assertIn("priority", by_slug["beta"])
        self.assertNotEqual(
            by_slug["beta"]["priority"], by_slug["alpha"]["priority"]
        )

    def test_hides_auxiliary_models(self) -> None:
        built = catalog.build_catalog(
            ["alpha", "codex-auto-review", "gpt-image-2"], {}
        )
        visibility = {entry["slug"]: entry["visibility"] for entry in built["models"]}
        self.assertEqual(visibility["alpha"], "list")
        self.assertEqual(visibility["codex-auto-review"], "hide")
        self.assertEqual(visibility["gpt-image-2"], "hide")

    def test_prefers_extra_sources_over_previous_catalog(self) -> None:
        previous = {"alpha": {"slug": "alpha", "display_name": "old", "priority": 1}}
        cache = {"alpha": {"slug": "alpha", "display_name": "new", "priority": 1}}
        built = catalog.build_catalog(["alpha"], previous, extra_sources=(cache,))
        self.assertEqual(built["models"][0]["display_name"], "new")

    def test_ignores_provider_models_that_are_no_longer_served(self) -> None:
        known = {"gone": {"slug": "gone", "display_name": "Gone", "priority": 1}}
        built = catalog.build_catalog(["alpha"], known)
        self.assertEqual([entry["slug"] for entry in built["models"]], ["alpha"])


class CatalogIsCurrentTests(unittest.TestCase):
    def test_missing_or_empty_catalog_is_out_of_date(self) -> None:
        desired = catalog.build_catalog(["alpha"], {})
        self.assertFalse(catalog.catalog_is_current(None, desired))
        self.assertFalse(catalog.catalog_is_current({}, desired))
        self.assertFalse(catalog.catalog_is_current({"models": []}, desired))

    def test_ignores_synced_at_timestamp(self) -> None:
        desired = catalog.build_catalog(["alpha"], {})
        stored = dict(desired)
        stored["synced_at"] = "2000-01-01T00:00:00Z"
        self.assertTrue(catalog.catalog_is_current(stored, desired))

    def test_detects_added_removed_and_rehidden_models(self) -> None:
        desired = catalog.build_catalog(["alpha", "codex-auto-review"], {})
        extra = catalog.build_catalog(["alpha", "codex-auto-review", "beta"], {})
        self.assertFalse(catalog.catalog_is_current(extra, desired))

        wrong_visibility = json.loads(json.dumps(desired))
        wrong_visibility["models"][1]["visibility"] = "list"
        self.assertFalse(catalog.catalog_is_current(wrong_visibility, desired))


class EnsureCatalogSettingTests(unittest.TestCase):
    def test_inserts_setting_before_the_first_table(self) -> None:
        text = 'model = "alpha"\n\n[model_providers.custom]\nname = "Custom"\n'
        updated = catalog.ensure_catalog_setting(text, Path("C:/tmp/catalog.json"))
        self.assertLess(updated.index("model_catalog_json"), updated.index("[model_providers.custom]"))
        self.assertTrue(catalog.setting_is_configured(updated, Path("C:/tmp/catalog.json")))

    def test_updates_existing_setting_in_place(self) -> None:
        text = 'model_catalog_json = "C:/old/catalog.json"\nmodel = "alpha"\n'
        updated = catalog.ensure_catalog_setting(text, Path("C:/new/catalog.json"))
        self.assertIn('model_catalog_json = "C:/new/catalog.json"', updated)
        self.assertNotIn("C:/old/catalog.json", updated)
        self.assertEqual(updated.count("model_catalog_json"), 1)

    def test_is_idempotent(self) -> None:
        text = 'model = "alpha"\n'
        once = catalog.ensure_catalog_setting(text, Path("C:/tmp/catalog.json"))
        twice = catalog.ensure_catalog_setting(once, Path("C:/tmp/catalog.json"))
        self.assertEqual(once, twice)

    def test_preserves_crlf_line_endings(self) -> None:
        text = 'model = "alpha"\r\n\r\n[model_providers.custom]\r\nname = "Custom"\r\n'
        updated = catalog.ensure_catalog_setting(text, Path("C:/tmp/catalog.json"))
        self.assertIn('\r\nmodel_catalog_json = "C:/tmp/catalog.json"\r\n', updated)
        self.assertNotIn("\n", updated.replace("\r\n", ""))

    def test_recognizes_windows_backslash_paths(self) -> None:
        text = 'model_catalog_json = "C:\\\\Users\\\\me\\\\.codex\\\\model_catalog.json"\nmodel = "alpha"\n'
        self.assertTrue(
            catalog.setting_is_configured(text, Path("C:/Users/me/.codex/model_catalog.json"))
        )

    def test_setting_after_a_table_is_not_accepted(self) -> None:
        text = '[model_providers.custom]\nmodel_catalog_json = "C:/tmp/catalog.json"\n'
        self.assertFalse(catalog.setting_is_configured(text, Path("C:/tmp/catalog.json")))


class ResolveProviderTests(unittest.TestCase):
    def test_uses_the_configured_provider(self) -> None:
        config = {"model_provider": "custom", "model_providers": {"custom": {"base_url": "x"}}}
        provider_id, provider = catalog.resolve_provider(config)
        self.assertEqual(provider_id, "custom")
        self.assertEqual(provider["base_url"], "x")

    def test_falls_back_to_a_single_provider(self) -> None:
        config = {"model_providers": {"only": {"base_url": "x"}}}
        self.assertEqual(catalog.resolve_provider(config)[0], "only")

    def test_rejects_configs_without_providers(self) -> None:
        with self.assertRaises(catalog.CatalogError):
            catalog.resolve_provider({"model_provider": "custom"})


class MainTests(unittest.TestCase):
    def test_writes_catalog_and_configures_setting(self) -> None:
        payload = {"data": [{"id": "alpha"}, {"id": "beta"}, {"id": "codex-auto-review"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                CONFIG_TEMPLATE.format(base_url="http://127.0.0.1:1/v1"),
                encoding="utf-8",
            )

            with patch.object(
                catalog.urllib.request, "urlopen", fake_urlopen(payload)
            ):
                self.assertEqual(catalog.main(["--codex-home", str(root)]), 0)

            stored = json.loads((root / "model_catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["slug"] for entry in stored["models"]],
                ["alpha", "beta", "codex-auto-review"],
            )
            updated = config_path.read_text(encoding="utf-8")
            self.assertTrue(
                catalog.setting_is_configured(updated, root / "model_catalog.json")
            )
            self.assertEqual(len(list(root.glob("config.toml.*.bak"))), 1)

    def test_check_only_never_writes(self) -> None:
        payload = {"data": [{"id": "alpha"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            original = CONFIG_TEMPLATE.format(base_url="http://127.0.0.1:1/v1")
            config_path.write_text(original, encoding="utf-8")

            with patch.object(
                catalog.urllib.request, "urlopen", fake_urlopen(payload)
            ):
                self.assertEqual(
                    catalog.main(["--check-only", "--codex-home", str(root)]), 0
                )

            self.assertFalse((root / "model_catalog.json").exists())
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(root.glob("*.bak")), [])

    def test_unreachable_provider_reports_an_error(self) -> None:
        def boom(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.toml").write_text(
                CONFIG_TEMPLATE.format(base_url="http://127.0.0.1:1/v1"),
                encoding="utf-8",
            )
            with patch.object(catalog.urllib.request, "urlopen", boom):
                self.assertEqual(catalog.main(["--codex-home", str(root)]), 1)
            self.assertFalse((root / "model_catalog.json").exists())

    def test_second_run_is_idempotent(self) -> None:
        payload = {"data": [{"id": "alpha"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.toml"
            config_path.write_text(
                CONFIG_TEMPLATE.format(base_url="http://127.0.0.1:1/v1"),
                encoding="utf-8",
            )
            with patch.object(
                catalog.urllib.request, "urlopen", fake_urlopen(payload)
            ):
                self.assertEqual(catalog.main(["--codex-home", str(root)]), 0)
                first_config = config_path.read_text(encoding="utf-8")
                first_catalog = (root / "model_catalog.json").read_text(encoding="utf-8")
                self.assertEqual(catalog.main(["--codex-home", str(root)]), 0)

            self.assertEqual(config_path.read_text(encoding="utf-8"), first_config)
            self.assertEqual(
                (root / "model_catalog.json").read_text(encoding="utf-8"),
                first_catalog,
            )
            self.assertEqual(len(list(root.glob("config.toml.*.bak"))), 1)


if __name__ == "__main__":
    unittest.main()
