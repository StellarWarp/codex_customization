import hashlib
import json
import shutil
import struct
import unittest
import uuid
from pathlib import Path

import patch_codex_model_experience_copy as patcher


REQUEST_NAME = "read-service-tier-for-request-TEST.js"
UI_NAME = "use-service-tier-settings-TEST.js"
QUERIES_NAME = "model-queries-TEST.js"
FILTER_NAME = "model-list-filter-TEST.js"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def integrity(data: bytes) -> dict:
    digest = sha256(data)
    return {
        "algorithm": "SHA256",
        "hash": digest,
        "blockSize": 4 * 1024 * 1024,
        "blocks": [digest],
    }


def make_test_assets() -> dict[str, bytes]:
    return {
        REQUEST_NAME: (
            "import{x}from'./x.js';"
            "async function T(e,t){let n=await x(e,t);"
            "if(n!==`chatgpt`)return!1;"
            "let r=await v(t,{priority:`critical`});"
            "return e.query.setData(g,{authMethod:n,hostId:t},r),"
            "r.requirements?.featureRequirements?.fast_mode!==!1}"
        ).encode(),
        UI_NAME: (
            "import{x}from'./x.js';function J(){"
            "let{data:u,isPending:d}=s(j,l),"
            "f=!!a?.isLoading||o&&d,"
            "p=o&&!f&&u!=null&&u?.requirements?.featureRequirements?.fast_mode!==!1;"
            "return{isServiceTierAllowed:p,isLoading:f}}"
        ).encode(),
        QUERIES_NAME: b"const W={availableModels:new Set(),useHiddenModels:!1,defaultModel:null};",
        FILTER_NAME: (
            "function r({models:o,useHiddenModels:s}){"
            "let u=s;return o.filter(r=>{"
            "if(u?n.has(r.model):!r.hidden){"
            "return r.supportedReasoningEfforts}})}"
        ).encode(),
    }


def write_test_asar(path: Path, assets: dict[str, bytes]) -> None:
    offset = 0
    entries = {}
    payload = bytearray()
    for name, data in assets.items():
        entries[name] = {
            "size": len(data),
            "offset": str(offset),
            "integrity": integrity(data),
        }
        payload.extend(data)
        offset += len(data)
    header = {
        "files": {
            "webview": {
                "files": {
                    "assets": {
                        "files": entries,
                    }
                }
            }
        }
    }
    header_bytes = json.dumps(header, separators=(",", ":")).encode()
    aligned = patcher.align4(len(header_bytes))
    prefix = struct.pack("<IIII", 4, aligned + 8, aligned + 4, len(header_bytes))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + header_bytes + (b"\x00" * (aligned - len(header_bytes))) + payload)


def make_package(root: Path, assets: dict[str, bytes] | None = None) -> tuple[Path, Path]:
    package_root = root / "OpenAI.Codex_TEST"
    app_dir = package_root / "app"
    write_test_asar(app_dir / "resources" / "app.asar", assets or make_test_assets())
    (app_dir / "ChatGPT.exe").write_bytes(b"test launcher")
    manifest = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">
  <Identity Name="OpenAI.Codex" Version="1.2.3.4" />
  <Applications>
    <Application Id="App" Executable="app/ChatGPT.exe" EntryPoint="Windows.FullTrustApplication" />
  </Applications>
</Package>
"""
    (package_root / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    return package_root, app_dir


class ModelExperiencePatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / "_test_workspace" / uuid.uuid4().hex
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_load_asar_uses_aligned_data_base(self) -> None:
        _, app_dir = make_package(self.root)
        archive = patcher.load_asar(app_dir / "resources" / "app.asar")
        self.assertEqual(archive.data_base, 8 + archive.outer_pickle_size)
        self.assertEqual(archive.data_base, 16 + patcher.align4(archive.header_size))
        self.assertTrue(archive.read_asset(REQUEST_NAME).startswith(b"import"))

    def test_plan_contains_four_equal_length_patches(self) -> None:
        _, app_dir = make_package(self.root)
        plan = patcher.build_patch_plan(
            app_dir,
            check_javascript=False,
            scan_embedded_hashes=False,
        )
        self.assertEqual(len(plan.asset_patches), 4)
        self.assertEqual(len(plan.archive.header_bytes), len(plan.header_after))
        self.assertNotEqual(plan.old_header_sha256, plan.new_header_sha256)
        for asset_patch in plan.asset_patches:
            self.assertEqual(len(asset_patch.old_bytes), len(asset_patch.new_bytes))
            self.assertNotEqual(asset_patch.old_sha256, asset_patch.new_sha256)

    def test_apply_updates_assets_header_and_embedded_hash(self) -> None:
        _, source_app = make_package(self.root)
        initial_archive = patcher.load_asar(source_app / "resources" / "app.asar")
        binary = source_app / "test-integrity.dll"
        binary.write_bytes(b"before:" + initial_archive.header_sha256.encode() + b":after")

        plan = patcher.build_patch_plan(
            source_app,
            check_javascript=False,
            scan_embedded_hashes=True,
        )
        self.assertEqual(len(plan.embedded_references), 1)
        output_app = self.root / "portable-app"
        shutil.copytree(source_app, output_app)
        patcher.apply_patch_plan(plan, source_app, output_app)

        archive = patcher.load_asar(output_app / "resources" / "app.asar")
        self.assertEqual(archive.header_sha256, plan.new_header_sha256)
        request = archive.read_asset(REQUEST_NAME).decode()
        ui = archive.read_asset(UI_NAME).decode()
        queries = archive.read_asset(QUERIES_NAME).decode()
        model_filter = archive.read_asset(FILTER_NAME).decode()
        self.assertIn("CFM_REQ", request)
        self.assertNotIn("if(n!==`chatgpt`)return!1;", request)
        self.assertIn("||d   ,p=1&&!f", ui)
        self.assertIn("useHiddenModels:!0", queries)
        self.assertIn("CFM_ALLOW", model_filter)
        self.assertNotIn("u?n.has(r.model):!r.hidden", model_filter)
        self.assertIn(plan.new_header_sha256.encode(), (output_app / binary.name).read_bytes())
        self.assertNotIn(plan.old_header_sha256.encode(), (output_app / binary.name).read_bytes())

    def test_replace_output_discards_old_content_after_verified_stage(self) -> None:
        _, source_app = make_package(self.root)
        plan = patcher.build_patch_plan(
            source_app,
            check_javascript=False,
            scan_embedded_hashes=False,
        )
        output_app = self.root / "CodexPortable"
        (output_app / "codex-portable-old").mkdir(parents=True)
        (output_app / "codex-portable-old" / "old.txt").write_text("old")

        patcher.install_or_replace_app_copy(
            plan,
            source_app,
            output_app,
            version="1.2.3.4",
            launcher_relative=Path("ChatGPT.exe"),
            replace_existing=True,
        )

        self.assertFalse((output_app / "codex-portable-old").exists())
        self.assertTrue((output_app / "ChatGPT.exe").is_file())
        marker = json.loads(
            (output_app / ".codex-portable-managed.json").read_text(encoding="ascii")
        )
        self.assertEqual(marker["store_version"], "1.2.3.4")
        leftovers = list(self.root.glob(".CodexPortable.*-*"))
        self.assertEqual(leftovers, [])

    def test_replace_output_keeps_old_content_when_staging_fails(self) -> None:
        _, source_app = make_package(self.root)
        plan = patcher.build_patch_plan(
            source_app,
            check_javascript=False,
            scan_embedded_hashes=False,
        )
        output_app = self.root / "CodexPortable"
        output_app.mkdir()
        (output_app / "old.txt").write_text("keep me")

        with self.assertRaisesRegex(patcher.PatchError, "staged launcher"):
            patcher.install_or_replace_app_copy(
                plan,
                source_app,
                output_app,
                version="1.2.3.4",
                launcher_relative=Path("Missing.exe"),
                replace_existing=True,
            )

        self.assertEqual((output_app / "old.txt").read_text(), "keep me")
        leftovers = list(self.root.glob(".CodexPortable.*-*"))
        self.assertEqual(leftovers, [])

    def test_ambiguous_request_gate_fails_without_writing(self) -> None:
        assets = make_test_assets()
        assets[REQUEST_NAME] += b"if(q!==`chatgpt`)return!1;"
        _, app_dir = make_package(self.root, assets)
        before = (app_dir / "resources" / "app.asar").read_bytes()
        with self.assertRaisesRegex(patcher.PatchError, "ambiguous"):
            patcher.build_patch_plan(
                app_dir,
                check_javascript=False,
                scan_embedded_hashes=False,
            )
        self.assertEqual(before, (app_dir / "resources" / "app.asar").read_bytes())

    def test_output_path_must_be_new_and_outside_source(self) -> None:
        _, app_dir = make_package(self.root)
        with self.assertRaises(patcher.PatchError):
            patcher.validate_output_path(app_dir, app_dir)
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaises(patcher.PatchError):
            patcher.validate_output_path(app_dir, existing)
        with self.assertRaises(patcher.PatchError):
            patcher.remove_tree_safely(self.root, self.root)

    def test_manifest_launcher_is_relative_to_copied_app(self) -> None:
        package_root, _ = make_package(self.root)
        version, launcher = patcher.read_package_metadata(package_root)
        self.assertEqual(version, "1.2.3.4")
        self.assertEqual(launcher, Path("ChatGPT.exe"))


if __name__ == "__main__":
    unittest.main()
