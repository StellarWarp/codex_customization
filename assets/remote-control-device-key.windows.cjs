"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const ALGORITHM = "ecdsa_p256_sha256";
const PROTECTION_CLASS = "os_protected_nonextractable";
const STORE_PATH = path.join(
  process.env.CODEX_HOME || path.join(os.homedir(), ".codex"),
  "remote-control-device-keys.windows.json",
);
const SHIM_VERSION = "codex-windows-device-key-shim-v1";

function dpapi(operation, base64Input) {
  const command =
    "Add-Type -AssemblyName System.Security; " +
    "$d=[Convert]::FromBase64String([Console]::In.ReadToEnd()); " +
    "[Console]::Out.Write([Convert]::ToBase64String(" +
    "[System.Security.Cryptography.ProtectedData]::" +
    operation +
    "($d,$null,[System.Security.Cryptography.DataProtectionScope]::CurrentUser)))";

  return execFileSync(
    "powershell.exe",
    ["-NoProfile", "-NonInteractive", "-Command", command],
    {
      input: base64Input,
      encoding: "utf8",
      windowsHide: true,
      timeout: 15000,
      maxBuffer: 4 * 1024 * 1024,
    },
  ).trim();
}

function readStore() {
  try {
    const parsed = JSON.parse(fs.readFileSync(STORE_PATH, "utf8"));
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("device key store must contain a JSON object");
    }
    return parsed;
  } catch (error) {
    if (error?.code === "ENOENT") return {};
    throw new Error(`Could not read Windows remote-control device keys: ${error.message}`);
  }
}

function writeStore(store) {
  fs.mkdirSync(path.dirname(STORE_PATH), { recursive: true });
  fs.writeFileSync(STORE_PATH, `${JSON.stringify(store, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    fs.chmodSync(STORE_PATH, 0o600);
  } catch {}
}

function requireEntry(keyId) {
  const entry = readStore()[keyId];
  if (!entry) throw new Error(`Remote-control device key not found: ${keyId}`);
  return entry;
}

function publicView(entry) {
  return {
    algorithm: entry.algorithm,
    keyId: entry.keyId,
    protectionClass: entry.protectionClass,
    publicKeySpkiDerBase64: entry.publicKeySpkiDerBase64,
  };
}

module.exports = {
  shimVersion: SHIM_VERSION,

  async createDeviceKey() {
    const pair = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
    const keyId = `dk_osn_${crypto.randomBytes(16).toString("hex")}`;
    const entry = {
      algorithm: ALGORITHM,
      keyId,
      protectionClass: PROTECTION_CLASS,
      publicKeySpkiDerBase64: pair.publicKey
        .export({ type: "spki", format: "der" })
        .toString("base64"),
    };
    const privateKeyBase64 = Buffer.from(
      pair.privateKey.export({ type: "pkcs8", format: "pem" }),
      "utf8",
    ).toString("base64");
    const store = readStore();
    store[keyId] = {
      ...entry,
      encryptedPrivateKeyBase64: dpapi("Protect", privateKeyBase64),
    };
    writeStore(store);
    return publicView(entry);
  },

  async deleteDeviceKey(keyId) {
    const store = readStore();
    delete store[keyId];
    writeStore(store);
  },

  async getDeviceKeyPublic(keyId) {
    return publicView(requireEntry(keyId));
  },

  async signDeviceKey(keyId, payload) {
    const entry = requireEntry(keyId);
    const privateKeyPem = Buffer.from(
      dpapi("Unprotect", entry.encryptedPrivateKeyBase64),
      "base64",
    ).toString("utf8");
    return {
      algorithm: ALGORITHM,
      signatureDerBase64: crypto
        .sign("sha256", Buffer.from(payload), privateKeyPem)
        .toString("base64"),
    };
  },
};
