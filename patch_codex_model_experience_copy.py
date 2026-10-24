#!/usr/bin/env python3
"""Create and patch a copy of the Windows Codex Desktop app directory.

The source Store/MSIX package is always treated as read-only. By default the
tool only analyzes the installed package. Passing --apply copies the complete
app directory to a new output directory and patches that copy in place.

The patch enables the Fast Mode features that are locally present but gated
for non-ChatGPT authentication:

* Fast Mode request eligibility
* Fast Mode UI eligibility

The ASAR file is patched with byte-for-byte equal-length replacements. The
script also updates the per-file and per-block SHA-256 values in the ASAR JSON
header without changing the header length or any subsequent file offset.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class PatchError(RuntimeError):
    """Raised when the package shape is unsafe or unsupported."""


IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
WINDOWS_APPS_PART = "windowsapps"
EXECUTABLE_SUFFIXES = {".exe", ".dll"}


@dataclass(frozen=True)
class AsarArchive:
    path: Path
    prefix: bytes
    outer_pickle_size: int
    header_size: int
    header_bytes: bytes
    header: dict
    data_base: int

    @property
    def header_sha256(self) -> str:
        return sha256_hex(self.header_bytes)

    def asset_entries(self) -> dict[str, dict]:
        try:
            return self.header["files"]["webview"]["files"]["assets"]["files"]
        except (KeyError, TypeError) as exc:
            raise PatchError("ASAR does not contain webview/assets") from exc

    def asset_entry(self, name: str) -> dict:
        return self.file_entry(f"webview/assets/{name}")

    def file_entry(self, archive_path: str) -> dict:
        try:
            entry = self.header
            for part in archive_path.split("/"):
                entry = entry["files"][part]
            return entry
        except (KeyError, TypeError) as exc:
            raise PatchError(f"ASAR file disappeared: {archive_path}") from exc

    def read_asset(self, name: str) -> bytes:
        return self.read_file(f"webview/assets/{name}")

    def read_file(self, archive_path: str) -> bytes:
        entry = self.file_entry(archive_path)
        try:
            offset = int(entry["offset"])
            size = int(entry["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchError(f"invalid ASAR entry for {archive_path}") from exc
        with self.path.open("rb") as handle:
            handle.seek(self.data_base + offset)
            data = handle.read(size)
        if len(data) != size:
            raise PatchError(
                f"short ASAR read for {archive_path}: {len(data)} != {size}"
            )
        return data

    def absolute_asset_offset(self, name: str) -> int:
        return self.absolute_file_offset(f"webview/assets/{name}")

    def absolute_file_offset(self, archive_path: str) -> int:
        return self.data_base + int(self.file_entry(archive_path)["offset"])

    def iter_file_entries(self):
        def walk(node: dict, prefix: str = ""):
            for name, entry in node.get("files", {}).items():
                archive_path = f"{prefix}/{name}" if prefix else name
                if "files" in entry:
                    yield from walk(entry, archive_path)
                else:
                    yield archive_path, entry

        yield from walk(self.header)


@dataclass(frozen=True)
class AssetPatch:
    feature: str
    name: str
    old_bytes: bytes
    new_bytes: bytes
    old_sha256: str
    new_sha256: str
    old_blocks: tuple[str, ...]
    new_blocks: tuple[str, ...]
    block_size: int


@dataclass(frozen=True)
class EmbeddedHashReference:
    relative_path: Path
    offsets: tuple[int, ...]


@dataclass(frozen=True)
class PatchPlan:
    archive: AsarArchive
    asset_patches: tuple[AssetPatch, ...]
    header_after: bytes
    embedded_references: tuple[EmbeddedHashReference, ...]

    @property
    def old_header_sha256(self) -> str:
        return self.archive.header_sha256

    @property
    def new_header_sha256(self) -> str:
        return sha256_hex(self.header_after)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def align4(value: int) -> int:
    return (value + 3) & ~3


def load_asar(path: Path) -> AsarArchive:
    path = path.resolve()
    with path.open("rb") as handle:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise PatchError("ASAR is too small to contain a pickle header")
        first, outer_pickle_size, inner_pickle_size, header_size = struct.unpack(
            "<IIII", prefix
        )
        if first != 4:
            raise PatchError(f"unsupported ASAR pickle prefix: {first}")
        if header_size <= 0:
            raise PatchError(f"invalid ASAR JSON header size: {header_size}")
        header_bytes = handle.read(header_size)
        if len(header_bytes) != header_size:
            raise PatchError("could not read complete ASAR JSON header")

    try:
        header = json.loads(header_bytes.rstrip(b"\x00").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchError("ASAR JSON header is not valid UTF-8 JSON") from exc

    data_base = 8 + outer_pickle_size
    aligned_base = 16 + align4(header_size)
    if data_base != aligned_base:
        raise PatchError(
            "unsupported ASAR pickle layout: "
            f"data base {data_base} != aligned header base {aligned_base}"
        )
    if inner_pickle_size != align4(header_size) + 4:
        raise PatchError(
            "unsupported ASAR inner pickle size: "
            f"{inner_pickle_size} != {align4(header_size) + 4}"
        )

    archive = AsarArchive(
        path=path,
        prefix=prefix,
        outer_pickle_size=outer_pickle_size,
        header_size=header_size,
        header_bytes=header_bytes,
        header=header,
        data_base=data_base,
    )
    _validate_archive_bounds(archive)
    return archive


def _validate_archive_bounds(archive: AsarArchive) -> None:
    file_size = archive.path.stat().st_size
    for name, entry in archive.iter_file_entries():
        if entry.get("unpacked"):
            continue
        try:
            start = archive.data_base + int(entry["offset"])
            end = start + int(entry["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PatchError(f"invalid ASAR asset bounds for {name}") from exc
        if start < archive.data_base or end > file_size:
            raise PatchError(f"ASAR asset is outside the archive: {name}")


def find_single_asset(archive: AsarArchive, prefix: str) -> str:
    matches = sorted(name for name in archive.asset_entries() if name.startswith(prefix))
    if len(matches) != 1:
        raise PatchError(
            f"expected one asset matching {prefix}*, found {len(matches)}: {matches}"
        )
    return matches[0]


def find_assets_for_patch(
    archive: AsarArchive,
    *,
    feature: str,
    legacy_prefix: str | None,
    required_needles: Sequence[bytes],
    patcher,
) -> tuple[str, ...]:
    """Locate every asset that carries a patchable gate for ``feature``.

    Older Codex builds shipped one small, lazily loaded chunk per gate, so the
    ``legacy_prefix`` lookup is still preferred when it matches. Current builds
    merge those chunks into the large ``app-initial-*`` and ``app-shared-*``
    bundles, so one gate can legitimately live in more than one asset.
    """

    if legacy_prefix is not None:
        legacy_matches = sorted(
            name for name in archive.asset_entries() if name.startswith(legacy_prefix)
        )
        if len(legacy_matches) == 1:
            return (legacy_matches[0],)
        if len(legacy_matches) > 1:
            raise PatchError(
                f"expected at most one asset matching {legacy_prefix}*, "
                f"found {len(legacy_matches)}: {legacy_matches}"
            )

    content_matches: list[str] = []
    invalid_candidates: list[tuple[str, PatchError]] = []
    for name in sorted(archive.asset_entries()):
        if not name.endswith((".js", ".mjs", ".cjs")):
            continue
        data = archive.read_asset(name)
        if not all(needle in data for needle in required_needles):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            patcher(text)
        except PatchError as exc:
            invalid_candidates.append((name, exc))
            continue
        content_matches.append(name)

    if invalid_candidates:
        details = "; ".join(f"{name}: {error}" for name, error in invalid_candidates)
        raise PatchError(f"{feature} has unsupported content-matched assets: {details}")
    return tuple(content_matches)


def fixed_comment(total_length: int, marker: str) -> str:
    minimum = len(marker) + 4
    if total_length < minimum:
        raise PatchError(
            f"cannot fit comment marker {marker!r} in {total_length} bytes"
        )
    return "/*" + marker + ("x" * (total_length - minimum)) + "*/"


FAST_REQUEST_MARKER = "CFM_REQ"
FAST_SAVED_REQUEST_MARKER = "CFM2"
FAST_EXECUTOR_MARKER = "CFM_TIER"
FAST_UI_ACCESS_MARKER = "CFM_UI"

FAST_REQUEST_LEGACY_GATE = re.compile(
    rf"if\((?P<auth>{IDENT})!==`chatgpt`\)return!1;"
)
FAST_REQUEST_AUTH_GATE = re.compile(
    rf"if\((?P<auth>{IDENT})!==`chatgpt`&&(?P<auth2>{IDENT})!==`personalAccessToken`\)return!1;"
)
FAST_SAVED_REQUEST_GATE = re.compile(
    rf"if\((?P<guard>!await\s*(?P<probe>{IDENT})\(\)\|\|)"
    rf"\(await\s*(?P<client>{IDENT})\.sendRequest\(`configRequirements/read`,"
    rf"[^;]{{0,240}}?featureRequirements\?\.fast_mode===!1\)return null;"
)

# Codex 26.10x moved the Fast Mode gates onto an explicit auth-method allowlist
# and dropped the ``featureRequirements?.fast_mode`` probe from the request
# path. The gates now live in the app-initial bundle instead of the old
# read-service-tier/use-service-tier chunks.
FAST_REQUEST_ACCESS_GATE = re.compile(
    rf"if\((?P<auth>{IDENT})!==`chatgpt`&&(?P=auth)!==`personalAccessToken`"
    rf"&&(?P=auth)!==`amazonBedrock`\)return null;"
)
FAST_SAVED_ACCESS_PROBE = re.compile(
    rf"if\(!await\s*(?P<probe>{IDENT})\(\)\)return null;"
    rf"(?=\s*let\s+(?P<request>{IDENT})=await\s+(?P<client>{IDENT})"
    rf"\.sendRequest\(`configRequirements/read`,)"
)
FAST_EXECUTOR_ACCESS_GATE = re.compile(
    rf"return\s+(?P<account>{IDENT})\?\.type===`chatgpt`\|\|"
    rf"(?P=account)\?\.type===`amazonBedrock`\?"
    rf"(?P<tier>{IDENT})\.serviceTier:null"
)
FAST_UI_ACCESS_GATE = re.compile(
    rf"(?P<access>{IDENT})=(?P<state>{IDENT})\?\.authMethod===`chatgpt`\|\|"
    rf"(?P=state)\?\.authMethod===`personalAccessToken`\|\|"
    rf"(?P=state)\?\.authMethod===`amazonBedrock`"
)


def patch_fast_request(text: str) -> tuple[str, bool]:
    """Remove every auth guard that gates the Fast Mode service tier.

    Codex has shipped this guard in several shapes:

    * ``if(auth !== `chatgpt`) return !1;``
    * ``if(auth !== `chatgpt` && auth !== `personalAccessToken`) return !1;``
    * ``if(auth !== `chatgpt` && auth !== `personalAccessToken` &&
      auth !== `amazonBedrock`) return null;``
    * ``if(!await probe() || (...configRequirements...fast_mode === !1))
      return null;`` for the saved-service-tier reader.
    * ``return account?.type === `chatgpt` || account?.type ===
      `amazonBedrock` ? tier : null`` for turn execution.
    * ``if(!await probe()) return null;`` in the saved-service-tier reader.

    Every matched guard is replaced by an equal-length comment (or an
    equal-length expression plus comment) so the ASAR offsets and integrity
    metadata stay untouched. The host/model feature requirement check
    (``fast_mode !== !1``) is intentionally preserved.
    """

    edits: list[tuple[int, int, str]] = []

    if FAST_REQUEST_MARKER not in text:
        for label, pattern in (
            ("legacy", FAST_REQUEST_LEGACY_GATE),
            ("auth", FAST_REQUEST_AUTH_GATE),
            ("auth-method", FAST_REQUEST_ACCESS_GATE),
        ):
            matches = list(pattern.finditer(text))
            if len(matches) > 1:
                raise PatchError(
                    "Fast request gate is unsupported or ambiguous: "
                    f"expected at most one {label} auth early-return gate, "
                    f"found {len(matches)}"
                )
            for match in matches:
                edits.append(
                    (
                        match.start(),
                        match.end(),
                        fixed_comment(len(match.group(0)), FAST_REQUEST_MARKER),
                    )
                )

    if FAST_SAVED_REQUEST_MARKER not in text:
        matches = list(FAST_SAVED_REQUEST_GATE.finditer(text))
        if len(matches) > 1:
            raise PatchError(
                "Fast request gate is unsupported or ambiguous: "
                f"expected at most one saved-service-tier probe, found {len(matches)}"
            )
        for match in matches:
            guard = match.group("guard")
            edits.append(
                (
                    match.start("guard"),
                    match.end("guard"),
                    fixed_comment(len(guard), FAST_SAVED_REQUEST_MARKER),
                )
            )

        matches = list(FAST_SAVED_ACCESS_PROBE.finditer(text))
        if len(matches) > 1:
            raise PatchError(
                "Fast request gate is unsupported or ambiguous: "
                f"expected at most one saved-service-tier auth probe, "
                f"found {len(matches)}"
            )
        for match in matches:
            edits.append(
                (
                    match.start(),
                    match.end(),
                    fixed_comment(len(match.group(0)), FAST_SAVED_REQUEST_MARKER),
                )
            )

    if FAST_EXECUTOR_MARKER not in text:
        matches = list(FAST_EXECUTOR_ACCESS_GATE.finditer(text))
        if len(matches) > 1:
            raise PatchError(
                "Fast request gate is unsupported or ambiguous: "
                f"expected at most one executor account-type gate, "
                f"found {len(matches)}"
            )
        for match in matches:
            tier = match.group("tier")
            prefix = f"return {tier}.serviceTier"
            comment = fixed_comment(
                len(match.group(0)) - len(prefix), FAST_EXECUTOR_MARKER
            )
            edits.append((match.start(), match.end(), prefix + comment))

    if not edits:
        if (
            FAST_REQUEST_MARKER in text
            or FAST_SAVED_REQUEST_MARKER in text
            or FAST_EXECUTOR_MARKER in text
        ):
            return text, False
        raise PatchError(
            "Fast request gate is unsupported or ambiguous: expected an auth "
            "early-return gate, saved-service-tier probe, or account-type gate, "
            "found 0"
        )

    edits.sort()
    previous_end = -1
    for start, end, _replacement in edits:
        if start < previous_end:
            raise PatchError("Fast request gate replacements overlap")
        previous_end = end

    patched = text
    for start, end, replacement in reversed(edits):
        patched = patched[:start] + replacement + patched[end:]
    return patched, True


def patch_fast_ui(text: str) -> tuple[str, bool]:
    original = re.compile(
        rf"(?P<loading>{IDENT})=!!(?P<state>{IDENT})\?\.isLoading\|\|"
        rf"(?P<chat>{IDENT})&&(?P<pending>{IDENT}),"
        rf"(?P<allowed>{IDENT})=(?P=chat)&&!(?P=loading)&&"
        rf"(?P<data>{IDENT})!=null&&(?P=data)\?\.requirements\?\."
        r"featureRequirements\?\.fast_mode!==!1"
    )
    patched = re.compile(
        rf"(?P<loading>{IDENT})=!!(?P<state>{IDENT})\?\.isLoading\|\|"
        rf"(?P<pending>{IDENT})\s*,(?P<allowed>{IDENT})=1\s*&&!"
        rf"(?P=loading)&&(?P<data>{IDENT})!=null&&(?P=data)\?\.requirements\?\."
        r"featureRequirements\?\.fast_mode!==!1"
    )

    edits: list[tuple[int, int, str]] = []

    if FAST_UI_ACCESS_MARKER not in text:
        matches = list(FAST_UI_ACCESS_GATE.finditer(text))
        if len(matches) > 1:
            raise PatchError(
                "Fast UI gate is unsupported or ambiguous: "
                f"expected at most one auth-method allowlist, found {len(matches)}"
            )
        for match in matches:
            access = match.group("access")
            prefix = f"{access}=!0"
            comment = fixed_comment(
                len(match.group(0)) - len(prefix), FAST_UI_ACCESS_MARKER
            )
            edits.append((match.start(), match.end(), prefix + comment))

    if not patched.search(text):
        matches = list(original.finditer(text))
        if len(matches) > 1:
            raise PatchError(
                "Fast UI gate is unsupported or ambiguous: "
                f"expected one ChatGPT-only expression, found {len(matches)}"
            )
        for match in matches:
            values = match.groupdict()
            old_loading = (
                f"{values['loading']}=!!{values['state']}?.isLoading||"
                f"{values['chat']}&&{values['pending']}"
            )
            new_loading_base = (
                f"{values['loading']}=!!{values['state']}?.isLoading||"
                f"{values['pending']}"
            )
            if len(new_loading_base) > len(old_loading):
                raise PatchError(
                    "Fast UI loading replacement cannot remain equal-length"
                )
            new_loading = new_loading_base.ljust(len(old_loading))
            truthy = "1".ljust(len(values["chat"]))
            new_allowed = (
                f"{values['allowed']}={truthy}&&!{values['loading']}&&"
                f"{values['data']}!=null&&{values['data']}?.requirements?."
                "featureRequirements?.fast_mode!==!1"
            )
            replacement = new_loading + "," + new_allowed
            if len(replacement.encode("utf-8")) != len(
                match.group(0).encode("utf-8")
            ):
                raise PatchError("Fast UI replacement changed the UTF-8 byte length")
            edits.append((match.start(), match.end(), replacement))

    if not edits:
        if FAST_UI_ACCESS_MARKER in text or patched.search(text):
            return text, False
        raise PatchError(
            "Fast UI gate is unsupported or ambiguous: expected one ChatGPT-only "
            "expression or auth-method allowlist, found 0"
        )

    patched_text = text
    for start, end, replacement in reversed(sorted(edits)):
        patched_text = patched_text[:start] + replacement + patched_text[end:]
    return patched_text, True


def compose_patchers(patchers):
    def composed(text: str) -> tuple[str, bool]:
        changed = False
        for patcher in patchers:
            text, patch_changed = patcher(text)
            changed = changed or patch_changed
        return text, changed

    return composed


def compute_block_hashes(data: bytes, block_size: int) -> tuple[str, ...]:
    if block_size <= 0:
        raise PatchError(f"invalid ASAR integrity block size: {block_size}")
    if not data:
        return (sha256_hex(b""),)
    return tuple(
        sha256_hex(data[start : start + block_size])
        for start in range(0, len(data), block_size)
    )


def make_asset_patch(
    archive: AsarArchive,
    feature: str,
    name: str,
    patcher,
) -> AssetPatch | None:
    old_bytes = archive.read_file(name)
    entry = archive.file_entry(name)
    integrity = entry.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "SHA256":
        raise PatchError(f"{name} does not have supported SHA256 integrity metadata")
    try:
        expected_hash = str(integrity["hash"])
        block_size = int(integrity["blockSize"])
        expected_blocks = tuple(str(value) for value in integrity["blocks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PatchError(f"invalid integrity metadata for {name}") from exc

    old_hash = sha256_hex(old_bytes)
    old_blocks = compute_block_hashes(old_bytes, block_size)
    if old_hash != expected_hash or old_blocks != expected_blocks:
        raise PatchError(f"ASAR integrity metadata does not match asset bytes: {name}")
    try:
        text = old_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"target asset is not UTF-8: {name}") from exc
    new_text, changed = patcher(text)
    if not changed:
        return None
    new_bytes = new_text.encode("utf-8")
    if len(new_bytes) != len(old_bytes):
        raise PatchError(
            f"equal-length invariant failed for {name}: "
            f"{len(old_bytes)} -> {len(new_bytes)}"
        )
    return AssetPatch(
        feature=feature,
        name=name,
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        old_sha256=old_hash,
        new_sha256=sha256_hex(new_bytes),
        old_blocks=old_blocks,
        new_blocks=compute_block_hashes(new_bytes, block_size),
        block_size=block_size,
    )


def update_header_integrity(
    archive: AsarArchive, patches: Sequence[AssetPatch]
) -> bytes:
    replacements: dict[str, str] = {}
    expected_counts: Counter[str] = Counter()
    for patch in patches:
        old_values = (patch.old_sha256,) + patch.old_blocks
        new_values = (patch.new_sha256,) + patch.new_blocks
        if len(old_values) != len(new_values):
            raise PatchError(f"integrity block count changed for {patch.name}")
        for old, new in zip(old_values, new_values):
            previous = replacements.get(old)
            if previous is not None and previous != new:
                raise PatchError(
                    f"ambiguous integrity replacement for hash {old}: "
                    f"{previous} vs {new}"
                )
            replacements[old] = new
            expected_counts[old] += 1

    header_after = archive.header_bytes
    for old, new in replacements.items():
        old_bytes = old.encode("ascii")
        new_bytes = new.encode("ascii")
        actual_count = header_after.count(old_bytes)
        expected_count = expected_counts[old]
        if actual_count != expected_count:
            raise PatchError(
                "integrity hash is not uniquely attributable to the target asset: "
                f"{old} occurs {actual_count} times, expected {expected_count}"
            )
        header_after = header_after.replace(old_bytes, new_bytes)

    if len(header_after) != len(archive.header_bytes):
        raise PatchError("ASAR header length changed while updating integrity hashes")
    try:
        json.loads(header_after.rstrip(b"\x00").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchError("updated ASAR header is invalid JSON") from exc
    return header_after


def iter_binary_candidates(app_dir: Path) -> Iterable[Path]:
    for path in app_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in EXECUTABLE_SUFFIXES:
            yield path


def find_bytes_offsets(path: Path, needle: bytes) -> tuple[int, ...]:
    offsets: list[int] = []
    overlap = max(0, len(needle) - 1)
    position = 0
    tail = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            combined = tail + chunk
            base = position - len(tail)
            start = 0
            while True:
                found = combined.find(needle, start)
                if found < 0:
                    break
                absolute = base + found
                if not offsets or offsets[-1] != absolute:
                    offsets.append(absolute)
                start = found + 1
            position += len(chunk)
            tail = combined[-overlap:] if overlap else b""
    return tuple(offsets)


def find_embedded_header_references(
    app_dir: Path, header_sha256: str
) -> tuple[EmbeddedHashReference, ...]:
    needle = header_sha256.encode("ascii")
    references: list[EmbeddedHashReference] = []
    for path in iter_binary_candidates(app_dir):
        offsets = find_bytes_offsets(path, needle)
        if offsets:
            references.append(
                EmbeddedHashReference(path.relative_to(app_dir), offsets)
            )
    return tuple(references)


def verify_javascript_module(data: bytes, node_path: str | None) -> None:
    if node_path is None:
        raise PatchError("node was not found; pass --skip-node-check to bypass")
    completed = subprocess.run(
        [node_path, "--check", "--input-type=module", "-"],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise PatchError(f"patched JavaScript failed node --check: {stderr}")


def build_patch_plan(
    app_dir: Path,
    *,
    check_javascript: bool = True,
    node_path: str | None = None,
    scan_embedded_hashes: bool = True,
) -> PatchPlan:
    app_dir = app_dir.resolve()
    asar_path = app_dir / "resources" / "app.asar"
    archive = load_asar(asar_path)
    target_specs = [
        (
            "Fast request gate",
            "read-service-tier-for-request-",
            (b"featureRequirements?.fast_mode", b"chatgpt"),
            patch_fast_request,
        ),
        (
            "Fast UI gate",
            "use-service-tier-settings-",
            (b"isServiceTierAllowed", b"featureRequirements?.fast_mode"),
            patch_fast_ui,
        ),
        (
            "Fast request access gate",
            None,
            (b"Failed to read service tier for request", b"amazonBedrock"),
            patch_fast_request,
        ),
        (
            "Fast UI access gate",
            None,
            (b"serviceTierAccess", b"authMethod===`personalAccessToken`||"),
            patch_fast_ui,
        ),
    ]
    targets_by_asset: dict[str, list[tuple[str, object]]] = defaultdict(list)
    matched_any = False
    for feature, legacy_prefix, required_needles, patcher in target_specs:
        for name in find_assets_for_patch(
            archive,
            feature=feature,
            legacy_prefix=legacy_prefix,
            required_needles=required_needles,
            patcher=patcher,
        ):
            matched_any = True
            targets_by_asset[f"webview/assets/{name}"].append((feature, patcher))
    if not matched_any:
        raise PatchError(
            "no supported Fast Mode gate assets were found in this Codex build"
        )

    patches: list[AssetPatch] = []
    for name, grouped_targets in targets_by_asset.items():
        features = "; ".join(feature for feature, _patcher in grouped_targets)
        patch = make_asset_patch(
            archive,
            features,
            name,
            compose_patchers([patcher for _feature, patcher in grouped_targets]),
        )
        if patch is not None:
            if check_javascript:
                verify_javascript_module(patch.new_bytes, node_path)
            patches.append(patch)
    header_after = update_header_integrity(archive, patches)
    references = (
        find_embedded_header_references(app_dir, archive.header_sha256)
        if scan_embedded_hashes and patches
        else ()
    )
    return PatchPlan(
        archive=archive,
        asset_patches=tuple(patches),
        header_after=header_after,
        embedded_references=references,
    )


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def path_mentions_windowsapps(path: Path) -> bool:
    return any(part.lower() == WINDOWS_APPS_PART for part in path.resolve().parts)


def validate_output_location(source_app: Path, output_dir: Path) -> None:
    source_app = source_app.resolve()
    output_dir = output_dir.resolve()
    if path_mentions_windowsapps(output_dir):
        raise PatchError("refusing to create or patch an output under WindowsApps")
    if output_dir == source_app or path_is_within(output_dir, source_app):
        raise PatchError("output directory must not be the source app directory")
    if not output_dir.parent.exists():
        raise PatchError(f"output parent directory does not exist: {output_dir.parent}")
    if output_dir == Path(output_dir.anchor):
        raise PatchError("refusing to use a drive root as the output directory")


def validate_output_path(source_app: Path, output_dir: Path) -> None:
    validate_output_location(source_app, output_dir)
    if output_dir.exists():
        raise PatchError(f"output directory already exists: {output_dir}")


def copy_app_directory(source_app: Path, output_dir: Path) -> None:
    validate_output_path(source_app, output_dir)
    shutil.copytree(source_app, output_dir, copy_function=shutil.copy2)


def make_writable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IWRITE | stat.S_IREAD)


def remove_tree_safely(path: Path, required_parent: Path) -> None:
    path = path.resolve()
    required_parent = required_parent.resolve()
    if path == required_parent or not path_is_within(path, required_parent):
        raise PatchError(f"refusing to remove directory outside {required_parent}: {path}")

    def clear_readonly_and_retry(function, blocked_path, _excinfo) -> None:
        blocked = Path(blocked_path)
        blocked.chmod(blocked.stat().st_mode | stat.S_IWRITE | stat.S_IREAD)
        function(blocked_path)

    shutil.rmtree(path, onexc=clear_readonly_and_retry)


def write_install_marker(
    output_app: Path,
    version: str,
    source_package_root: Path,
) -> None:
    marker = output_app / ".codex-portable-managed.json"
    marker.write_text(
        json.dumps(
            {
                "managed_by": Path(__file__).name,
                "store_version": version,
                "source_package_root": str(source_package_root.resolve()),
            },
            indent=2,
        )
        + "\n",
        encoding="ascii",
    )


def apply_patch_plan(plan: PatchPlan, source_app: Path, output_app: Path) -> None:
    source_app = source_app.resolve()
    output_app = output_app.resolve()
    if output_app == source_app or path_is_within(output_app, source_app):
        raise PatchError("refusing to patch the source application directory")
    if path_mentions_windowsapps(output_app):
        raise PatchError("refusing to patch a WindowsApps path")

    output_asar = output_app / "resources" / "app.asar"
    current = load_asar(output_asar)
    if current.header_bytes != plan.archive.header_bytes:
        raise PatchError("copied ASAR header differs from the analyzed source")
    for patch in plan.asset_patches:
        if current.read_file(patch.name) != patch.old_bytes:
            raise PatchError(f"copied target differs from the analyzed source: {patch.name}")

    make_writable(output_asar)
    with output_asar.open("r+b") as handle:
        for patch in plan.asset_patches:
            handle.seek(current.absolute_file_offset(patch.name))
            handle.write(patch.new_bytes)
        handle.seek(16)
        handle.write(plan.header_after)
        handle.flush()
        os.fsync(handle.fileno())

    old_header = plan.old_header_sha256.encode("ascii")
    new_header = plan.new_header_sha256.encode("ascii")
    for reference in plan.embedded_references:
        binary_path = output_app / reference.relative_path
        make_writable(binary_path)
        with binary_path.open("r+b") as handle:
            for offset in reference.offsets:
                handle.seek(offset)
                if handle.read(len(old_header)) != old_header:
                    raise PatchError(
                        f"embedded header hash changed before patching: {binary_path}"
                    )
                handle.seek(offset)
                handle.write(new_header)
            handle.flush()
            os.fsync(handle.fileno())

    verify_applied_plan(plan, output_app)


def verify_applied_plan(plan: PatchPlan, output_app: Path) -> None:
    archive = load_asar(output_app / "resources" / "app.asar")
    if archive.header_bytes != plan.header_after:
        raise PatchError("patched ASAR header does not match the planned header")
    for patch in plan.asset_patches:
        data = archive.read_file(patch.name)
        if data != patch.new_bytes or sha256_hex(data) != patch.new_sha256:
            raise PatchError(f"patched asset verification failed: {patch.name}")
        integrity = archive.file_entry(patch.name)["integrity"]
        if integrity["hash"] != patch.new_sha256:
            raise PatchError(f"patched header hash is stale: {patch.name}")
        if tuple(integrity["blocks"]) != patch.new_blocks:
            raise PatchError(f"patched header block hashes are stale: {patch.name}")

    new_header = plan.new_header_sha256.encode("ascii")
    for reference in plan.embedded_references:
        binary_path = output_app / reference.relative_path
        with binary_path.open("rb") as handle:
            for offset in reference.offsets:
                handle.seek(offset)
                if handle.read(len(new_header)) != new_header:
                    raise PatchError(
                        f"embedded header hash verification failed: {binary_path}"
                    )

def install_or_replace_app_copy(
    plan: PatchPlan,
    source_app: Path,
    output_dir: Path,
    *,
    version: str,
    launcher_relative: Path,
    replace_existing: bool,
) -> None:
    source_app = source_app.resolve()
    output_dir = output_dir.resolve()
    validate_output_location(source_app, output_dir)

    if not replace_existing:
        copy_app_directory(source_app, output_dir)
        apply_patch_plan(plan, source_app, output_dir)
        write_install_marker(output_dir, version, source_app.parent)
        if not (output_dir / launcher_relative).is_file():
            raise PatchError(
                f"copied launcher does not exist: {output_dir / launcher_relative}"
            )
        return

    if output_dir.exists() and not output_dir.is_dir():
        raise PatchError(f"replacement target is not a directory: {output_dir}")

    token = uuid.uuid4().hex
    parent = output_dir.parent
    staging = parent / f".{output_dir.name}.staging-{token}"
    previous = parent / f".{output_dir.name}.previous-{token}"
    moved_previous = False
    installed_new = False

    try:
        copy_app_directory(source_app, staging)
        apply_patch_plan(plan, source_app, staging)
        write_install_marker(staging, version, source_app.parent)
        if not (staging / launcher_relative).is_file():
            raise PatchError(
                f"staged launcher does not exist: {staging / launcher_relative}"
            )

        if output_dir.exists():
            output_dir.rename(previous)
            moved_previous = True
        staging.rename(output_dir)
        installed_new = True

        if moved_previous:
            remove_tree_safely(previous, parent)
    except Exception:
        if not installed_new and moved_previous and not output_dir.exists():
            previous.rename(output_dir)
            moved_previous = False
        if staging.exists():
            remove_tree_safely(staging, parent)
        raise


def package_root_from_candidate(candidate: Path) -> Path | None:
    candidate = candidate.resolve()
    roots = candidate.parents if candidate.is_file() else (candidate, *candidate.parents)
    for root in roots:
        if (root / "AppxManifest.xml").is_file() and (root / "app").is_dir():
            return root
    return None


def discover_package_root() -> Path:
    configured_candidates: list[Path] = []
    configured_root = os.environ.get("CODEX_PACKAGE_ROOT")
    if configured_root:
        configured_candidates.append(Path(configured_root))

    portable_target = os.environ.get("CODEX_PORTABLE_TARGET")
    if portable_target:
        marker_path = Path(portable_target) / ".codex-portable-managed.json"
        try:
            marker = json.loads(marker_path.read_text(encoding="ascii"))
            marker_root = marker.get("source_package_root")
            if isinstance(marker_root, str) and marker_root:
                configured_candidates.append(Path(marker_root))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass

    for candidate in configured_candidates:
        root = package_root_from_candidate(candidate)
        if root is not None:
            return root

    command = (
        "$pkg = Get-AppxPackage -Name OpenAI.Codex | "
        "Sort-Object Version -Descending | Select-Object -First 1; "
        "if ($null -ne $pkg) { $pkg.InstallLocation }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            if line.strip():
                root = package_root_from_candidate(Path(line.strip()))
                if root is not None:
                    return root

    process_command = (
        "Get-Process -Name ChatGPT,codex -ErrorAction SilentlyContinue | "
        "ForEach-Object { try { if ($_.Path) { $_.Path } } catch {} }"
    )
    processes = subprocess.run(
        ["powershell", "-NoProfile", "-Command", process_command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if processes.returncode == 0:
        for line in processes.stdout.splitlines():
            if line.strip():
                root = package_root_from_candidate(Path(line.strip()))
                if root is not None:
                    return root

    detail = completed.stderr.strip() if completed.returncode != 0 else ""
    if detail:
        raise PatchError(f"OpenAI.Codex Store package was not found ({detail})")
    raise PatchError(
        "OpenAI.Codex Store package was not found and no running Codex process "
        "revealed its install location"
    )


def discover_node_path(package_root: Path) -> str | None:
    node_path = shutil.which("node")
    if node_path is not None:
        return node_path
    candidates = (
        package_root / "app" / "resources" / "cua_node" / "bin" / "node.exe",
        package_root / "app" / "resources" / "node.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


@contextmanager
def prepared_node_path(package_root: Path):
    system_node = shutil.which("node")
    if system_node is not None:
        yield system_node
        return

    bundled_node = discover_node_path(package_root)
    if bundled_node is None:
        yield None
        return

    with tempfile.TemporaryDirectory(prefix="codex-portable-node-") as temp_dir:
        temporary_node = Path(temp_dir) / "node.exe"
        shutil.copy2(bundled_node, temporary_node)
        yield str(temporary_node)


def read_package_metadata(package_root: Path) -> tuple[str, Path]:
    manifest_path = package_root / "AppxManifest.xml"
    try:
        root = ET.parse(manifest_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise PatchError(f"could not read AppxManifest.xml: {manifest_path}") from exc
    version = "unknown"
    executable: str | None = None
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "Identity" and element.get("Version"):
            version = element.get("Version", version)
        if local_name == "Application" and element.get("Executable"):
            executable = element.get("Executable")
            break
    if not executable:
        raise PatchError("AppxManifest.xml does not declare an application executable")
    normalized = Path(executable.replace("/", os.sep).replace("\\", os.sep))
    try:
        launcher_relative = normalized.relative_to("app")
    except ValueError as exc:
        raise PatchError(
            f"manifest executable is not inside the app directory: {executable}"
        ) from exc
    return version, launcher_relative


def print_plan(
    package_root: Path,
    app_dir: Path,
    version: str,
    launcher_relative: Path,
    plan: PatchPlan,
) -> None:
    print(f"Package root: {package_root}")
    print(f"Package version: {version}")
    print(f"Source app: {app_dir}")
    print(f"Portable launcher: {launcher_relative}")
    print(f"ASAR: {plan.archive.path}")
    print(f"ASAR data base: {plan.archive.data_base}")
    print(f"ASAR header SHA256 before: {plan.old_header_sha256}")
    print(f"ASAR header SHA256 after:  {plan.new_header_sha256}")
    if plan.asset_patches:
        print("Planned equal-length patches:")
        for patch in plan.asset_patches:
            print(
                f"  - {patch.feature}: {patch.name} "
                f"({len(patch.old_bytes)} bytes, {patch.old_sha256} -> {patch.new_sha256})"
            )
    else:
        print("All configured ASAR patches are already present.")
    if plan.embedded_references:
        print("Embedded ASAR header hash references:")
        for reference in plan.embedded_references:
            print(f"  - {reference.relative_path}: {len(reference.offsets)} occurrence(s)")
    else:
        print("Embedded ASAR header hash references: none found in EXE/DLL files")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze or create a copied Codex Desktop app with Fast Mode patches. "
            "The Store package is never modified."
        )
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        help="OpenAI.Codex package root; auto-detected with Get-AppxPackage by default",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy the complete app directory and patch the new copy",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory that will contain the copied app; required with --apply",
    )
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help=(
            "replace an existing output directory after a staged copy passes all "
            "patch and verification checks"
        ),
    )
    parser.add_argument(
        "--skip-node-check",
        action="store_true",
        help="skip node --check validation of patched JavaScript",
    )
    parser.add_argument(
        "--skip-embedded-hash-scan",
        action="store_true",
        help="skip scanning EXE/DLL files for an embedded ASAR header hash",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        package_root = (
            args.package_root.resolve() if args.package_root else discover_package_root()
        )
        app_dir = package_root / "app"
        if not app_dir.is_dir():
            raise PatchError(f"package app directory does not exist: {app_dir}")
        version, launcher_relative = read_package_metadata(package_root)
        if args.skip_node_check:
            plan = build_patch_plan(
                app_dir,
                check_javascript=False,
                scan_embedded_hashes=not args.skip_embedded_hash_scan,
            )
        else:
            with prepared_node_path(package_root) as node_path:
                plan = build_patch_plan(
                    app_dir,
                    check_javascript=True,
                    node_path=node_path,
                    scan_embedded_hashes=not args.skip_embedded_hash_scan,
                )
        print_plan(package_root, app_dir, version, launcher_relative, plan)

        if not args.apply:
            if args.replace_output:
                raise PatchError("--replace-output requires --apply")
            print("Dry run only: no files were copied or modified.")
            return 0
        if args.output_dir is None:
            raise PatchError("--output-dir is required with --apply")
        if not plan.asset_patches:
            raise PatchError("source package is already patched; refusing to create a copy")

        output_dir = args.output_dir.resolve()
        if args.replace_output:
            print(f"Staging and replacing portable app directory: {output_dir}")
        else:
            print(f"Copying complete app directory to: {output_dir}")
        install_or_replace_app_copy(
            plan,
            app_dir,
            output_dir,
            version=version,
            launcher_relative=launcher_relative,
            replace_existing=args.replace_output,
        )
        launcher = output_dir / launcher_relative
        print("Patch and verification completed successfully.")
        print(f"Launch manually with: {launcher}")
        return 0
    except PatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
