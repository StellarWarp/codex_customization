import hashlib
import json
import os
import shutil
import struct
import subprocess
import unittest
import uuid
from pathlib import Path

import patch_codex_model_experience_copy as patcher


REQUEST_NAME = "read-service-tier-for-request-TEST.js"
UI_NAME = "use-service-tier-settings-TEST.js"
QUERIES_NAME = "model-queries-TEST.js"
FILTER_NAME = "model-list-filter-TEST.js"
INITIAL_NAME = "app-initial-TEST.js"
REMOTE_NAME = "remote-connections-settings-TEST.js"
MAIN_PATH = ".vite/build/main-TEST.js"


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
        REMOTE_NAME: (
            "function H(){let m=r(`782640499`),g=r(x),R=S(),z=!m,"
            "V=build({showControlOtherDevices:z});"
            "return{showControlOtherDevices:z,value:V}}"
        ).encode(),
    }


def make_current_initial_assets() -> dict[str, bytes]:
    assets = make_test_assets()
    current_filter = (
        "function q({additionalAvailableModels:e,authMethod:t,availableModels:n,"
        "model:r,useHiddenModels:i}){return e?.has(r.model)===!0||"
        "(i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)}"
        "function filter(){return r.supportedReasoningEfforts}"
    ).encode()
    return {
        INITIAL_NAME: b"".join(
            assets[name]
            for name in (REQUEST_NAME, UI_NAME, QUERIES_NAME)
        )
        + current_filter,
    }


def make_test_main() -> bytes:
    return (
        "var A=createRequire(__filename),j=`remote-control-device-key.node`;"
        "class N{resourcesPath;addon=null;"
        "createDeviceKey(e){return this.getAddon().createDeviceKey(e??`hardware_only`)}"
        "deleteDeviceKey(e){return this.getAddon().deleteDeviceKey(e)}"
        "getDeviceKeyPublic(e){return this.getAddon().getDeviceKeyPublic(e)}"
        "signDeviceKey(e,t){return this.getAddon().signDeviceKey(e,t)}"
        "getAddon(){if(process.platform!==`darwin`)throw Error("
        "`Remote control device keys are only available on macOS`);"
        "if(this.resourcesPath==null)throw Error("
        "`Remote control device keys require resourcesPath`);"
        "return this.addon??=A((0,p.join)(this.resourcesPath,`native`,j)),this.addon}}"
    ).encode()


def write_test_asar(
    path: Path, assets: dict[str, bytes], extra_files: dict[str, bytes] | None = None
) -> None:
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
    for archive_path, data in (extra_files or {}).items():
        parts = archive_path.split("/")
        node = header
        for part in parts[:-1]:
            node = node.setdefault("files", {}).setdefault(part, {"files": {}})
        node.setdefault("files", {})[parts[-1]] = {
            "size": len(data),
            "offset": str(offset),
            "integrity": integrity(data),
        }
        payload.extend(data)
        offset += len(data)
    header_bytes = json.dumps(header, separators=(",", ":")).encode()
    aligned = patcher.align4(len(header_bytes))
    prefix = struct.pack("<IIII", 4, aligned + 8, aligned + 4, len(header_bytes))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + header_bytes + (b"\x00" * (aligned - len(header_bytes))) + payload)


def make_package(root: Path, assets: dict[str, bytes] | None = None) -> tuple[Path, Path]:
    package_root = root / "OpenAI.Codex_TEST"
    app_dir = package_root / "app"
    write_test_asar(
        app_dir / "resources" / "app.asar",
        assets or make_test_assets(),
        {MAIN_PATH: make_test_main()},
    )
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

    def test_remote_control_patch_is_disabled_by_default(self) -> None:
        _, app_dir = make_package(self.root)
        plan = patcher.build_patch_plan(
            app_dir,
            check_javascript=False,
            scan_embedded_hashes=False,
        )
        self.assertEqual(len(plan.asset_patches), 4)
        self.assertEqual(plan.external_files, ())
        self.assertNotIn(
            "CRC_UI", patcher.load_asar(app_dir / "resources" / "app.asar")
            .read_asset(REMOTE_NAME)
            .decode()
        )

    def test_opt_in_plan_contains_six_equal_length_patches_and_shim(self) -> None:
        _, app_dir = make_package(self.root)
        plan = patcher.build_patch_plan(
            app_dir,
            check_javascript=False,
            scan_embedded_hashes=False,
            enable_control_other_devices=True,
        )
        self.assertEqual(len(plan.asset_patches), 6)
        self.assertEqual(len(plan.external_files), 1)
        self.assertEqual(len(plan.archive.header_bytes), len(plan.header_after))
        self.assertNotEqual(plan.old_header_sha256, plan.new_header_sha256)
        for asset_patch in plan.asset_patches:
            self.assertEqual(len(asset_patch.old_bytes), len(asset_patch.new_bytes))
            self.assertNotEqual(asset_patch.old_sha256, asset_patch.new_sha256)

    def test_current_model_filter_in_merged_initial_bundle_is_patched(self) -> None:
        _, app_dir = make_package(self.root, make_current_initial_assets())
        plan = patcher.build_patch_plan(
            app_dir,
            check_javascript=False,
            scan_embedded_hashes=False,
        )
        self.assertEqual(len(plan.asset_patches), 1)
        asset_patch = plan.asset_patches[0]
        self.assertEqual(asset_patch.name, f"webview/assets/{INITIAL_NAME}")
        self.assertIn("CFM_ALLOW", asset_patch.new_bytes.decode())
        self.assertNotIn(
            "e?.has(r.model)===!0||(i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)",
            asset_patch.new_bytes.decode(),
        )

    def test_apply_updates_assets_header_and_embedded_hash(self) -> None:
        _, source_app = make_package(self.root)
        initial_archive = patcher.load_asar(source_app / "resources" / "app.asar")
        binary = source_app / "test-integrity.dll"
        binary.write_bytes(b"before:" + initial_archive.header_sha256.encode() + b":after")

        plan = patcher.build_patch_plan(
            source_app,
            check_javascript=False,
            scan_embedded_hashes=True,
            enable_control_other_devices=True,
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
        remote_settings = archive.read_asset(REMOTE_NAME).decode()
        main = archive.read_file(MAIN_PATH).decode()
        self.assertIn("CFM_REQ", request)
        self.assertNotIn("if(n!==`chatgpt`)return!1;", request)
        self.assertIn("||d   ,p=1&&!f", ui)
        self.assertIn("useHiddenModels:!0", queries)
        self.assertIn("CFM_ALLOW", model_filter)
        self.assertNotIn("u?n.has(r.model):!r.hidden", model_filter)
        self.assertIn("CRC_UI", remote_settings)
        self.assertIn("CRC_KEY", main)
        self.assertIn(patcher.DEVICE_KEY_SHIM_NAME, main)
        shim = output_app / "resources" / patcher.DEVICE_KEY_SHIM_NAME
        self.assertTrue(shim.is_file())
        self.assertEqual(
            patcher.sha256_hex(shim.read_bytes()), plan.external_files[0].sha256
        )
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

    def test_windows_device_key_shim_signs_and_deletes_with_dpapi(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available")
        shim = patcher.DEVICE_KEY_SHIM_SOURCE.resolve()
        script = r"""
const crypto = require("node:crypto");
const shim = require(process.argv[1]);
(async () => {
  const payload = Buffer.from("codex-device-key-test", "utf8");
  const created = await shim.createDeviceKey("allow_os_protected_nonextractable");
  const publicView = await shim.getDeviceKeyPublic(created.keyId);
  const signed = await shim.signDeviceKey(created.keyId, payload);
  const publicKey = crypto.createPublicKey({
    key: Buffer.from(publicView.publicKeySpkiDerBase64, "base64"),
    format: "der",
    type: "spki",
  });
  if (!crypto.verify("sha256", payload, publicKey, Buffer.from(signed.signatureDerBase64, "base64"))) {
    throw new Error("signature verification failed");
  }
  await shim.deleteDeviceKey(created.keyId);
  try {
    await shim.getDeviceKeyPublic(created.keyId);
    throw new Error("deleted key remained readable");
  } catch (error) {
    if (error.message === "deleted key remained readable") throw error;
  }
})().catch((error) => { console.error(error); process.exit(1); });
"""
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.root / "codex-home")
        completed = subprocess.run(
            [node, "-e", script, str(shim)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
