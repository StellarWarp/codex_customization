#!/usr/bin/env python3
"""Generate the Codex model catalog so a custom provider's models are pickable.

Codex Desktop builds the model picker from the app-server ``model/list`` call.
For a custom ``model_provider`` the app-server only returns the ChatGPT account
catalog, so models served by a local OpenAI-compatible gateway never show up in
the picker even though they are usable.

Codex exposes a supported hook for this: the top-level ``model_catalog_json``
setting points at a JSON catalog that replaces the account catalog. This script
builds that file from the models the active provider actually serves, keeps the
rich metadata already known for a slug, and ensures ``config.toml`` references
the catalog.

Only ``$CODEX_HOME/config.toml`` and ``$CODEX_HOME/model_catalog.json`` are
touched. The existing ``config.toml`` is backed up once per run before the first
write.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_TIMEOUT_SECONDS = 15.0
CATALOG_SETTING = "model_catalog_json"
HIDDEN_SLUG_PATTERNS = (
    re.compile(r"^codex-auto-review$"),
    re.compile(r"^gpt-image-"),
)

REASONING_LEVELS = (
    ("low", "Fast responses with lighter reasoning"),
    ("medium", "Balances speed and reasoning depth for everyday tasks"),
    ("high", "Greater reasoning depth for complex problems"),
    ("xhigh", "Extra high reasoning depth for complex problems"),
    ("max", "Maximum reasoning depth for the hardest problems"),
)
DEFAULT_INSTRUCTIONS_TEMPLATE = (
    "You are Codex, a coding agent. You and the user share one workspace."
)


class CatalogError(RuntimeError):
    """Raised when the catalog or configuration cannot be built safely."""


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def log(message: str) -> None:
    print(message, flush=True)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11
        raise CatalogError("Python 3.11+ with tomllib is required") from exc
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise CatalogError(f"could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"could not parse {path}: {exc}") from exc


def resolve_provider(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    provider_id = config.get("model_provider")
    providers = config.get("model_providers") or {}
    if not isinstance(providers, dict) or not providers:
        raise CatalogError("config.toml does not declare any model_providers")
    if not isinstance(provider_id, str) or provider_id not in providers:
        if len(providers) == 1:
            provider_id = next(iter(providers))
        elif "custom" in providers:
            provider_id = "custom"
        else:
            raise CatalogError(
                "config.toml does not select a usable model_provider "
                f"(model_provider={provider_id!r})"
            )
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise CatalogError(f"model_providers.{provider_id} is not a table")
    return provider_id, provider


def provider_bearer_token(provider: dict[str, Any]) -> str | None:
    token = provider.get("experimental_bearer_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    env_key = provider.get("env_key")
    if isinstance(env_key, str) and env_key.strip():
        value = os.environ.get(env_key.strip())
        if value:
            return value
    return None


def models_endpoint(base_url: str) -> str:
    trimmed = base_url.strip().rstrip("/")
    if not trimmed:
        raise CatalogError("the provider does not declare base_url")
    return f"{trimmed}/models"


def fetch_provider_models(base_url: str, token: str | None, timeout: float) -> list[str]:
    url = models_endpoint(base_url)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CatalogError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CatalogError(f"{url} is unreachable: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{url} did not return JSON: {exc}") from exc

    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise CatalogError(f"{url} did not return a model list")
    slugs: list[str] = []
    for entry in entries:
        slug = entry if isinstance(entry, str) else None
        if isinstance(entry, dict):
            slug = entry.get("id") or entry.get("slug") or entry.get("model")
        if isinstance(slug, str) and slug.strip():
            slugs.append(slug.strip())
    if not slugs:
        raise CatalogError(f"{url} returned no models")
    ordered: list[str] = []
    seen: set[str] = set()
    for slug in slugs:
        if slug not in seen:
            seen.add(slug)
            ordered.append(slug)
    return ordered


def load_catalog(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def catalog_entries(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    models = payload.get("models")
    if not isinstance(models, list):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for model in models:
        if isinstance(model, dict):
            slug = model.get("slug")
            if isinstance(slug, str) and slug:
                entries[slug] = model
    return entries


def is_hidden_slug(slug: str) -> bool:
    return any(pattern.search(slug) for pattern in HIDDEN_SLUG_PATTERNS)


def synthesize_entry(slug: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "display_name": slug,
        "description": slug,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": effort, "description": description}
            for effort, description in REASONING_LEVELS[:3]
        ],
        "shell_type": "unified_exec",
        "visibility": "hide" if is_hidden_slug(slug) else "list",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "model_messages": {
            "instructions_template": DEFAULT_INSTRUCTIONS_TEMPLATE,
            "instructions_variables": None,
            "approvals": None,
            "collaboration_modes": None,
            "auto_review": None,
            "permissions": None,
            "multi_agent": None,
        },
        "include_skills_usage_instructions": True,
        "include_plugin_usage_instructions": True,
        "include_apps_usage_instructions": True,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": None,
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_image_detail_original": True,
        "context_window": 272000,
        "max_context_window": 272000,
        "comp_hash": "2911",
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": False,
        "supports_experimental_context": False,
        "use_responses_lite": False,
        "node_repl_auto_review_required": False,
        "node_repl_disabled": False,
    }


def next_priority(entries: Iterable[dict[str, Any]]) -> int:
    highest = 0
    for entry in entries:
        value = entry.get("priority")
        if isinstance(value, int) and value > highest:
            highest = value
    return highest + 1


def build_catalog(
    slugs: Sequence[str],
    previous: dict[str, dict[str, Any]],
    extra_sources: Sequence[dict[str, dict[str, Any]]] = (),
) -> dict[str, Any]:
    """Merge freshly discovered slugs with the richest metadata known for each."""

    known: dict[str, dict[str, Any]] = {}
    for source in (*extra_sources, previous):
        for slug, entry in source.items():
            known.setdefault(slug, entry)

    models: list[dict[str, Any]] = []
    priority = next_priority(previous.values())
    for slug in slugs:
        entry = known.get(slug)
        if entry is None:
            entry = synthesize_entry(slug)
            entry["priority"] = priority
            priority += 1
        else:
            entry = dict(entry)
            entry.setdefault("slug", slug)
            if "priority" not in entry:
                entry["priority"] = priority
                priority += 1
        entry["visibility"] = "hide" if is_hidden_slug(slug) else "list"
        models.append(entry)

    models.sort(key=lambda item: (item.get("priority") or 0, item.get("slug") or ""))
    return {
        "synced_at": _datetime.datetime.now(_datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "models": models,
    }


def catalog_is_current(payload: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    """Report whether the catalog on disk already matches the desired content.

    Comparing the full entries (not just the slugs) means richer metadata that
    appears later, for example from ``models_cache.json``, is picked up on the
    next run while a steady state still reports "in sync".
    """

    if not payload:
        return False
    existing = payload.get("models")
    if not isinstance(existing, list) or not existing:
        return False
    return json.dumps(existing, sort_keys=True) == json.dumps(
        desired["models"], sort_keys=True
    )


def setting_is_configured(text: str, catalog_path: Path) -> bool:
    expected = catalog_path.as_posix()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            return False
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(rf"{CATALOG_SETTING}\s*=\s*[\"\']([^\"\']*)[\"\']", stripped)
        if match:
            return Path(match.group(1).replace("\\\\", "\\")).as_posix() == expected
    return False


def ensure_catalog_setting(text: str, catalog_path: Path) -> str:
    if setting_is_configured(text, catalog_path):
        return text

    value = catalog_path.as_posix()
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=False)
    pattern = re.compile(rf"^{CATALOG_SETTING}\s*=")
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            lines.insert(index, f'{CATALOG_SETTING} = "{value}"')
            return newline.join(lines) + newline
        if pattern.match(line):
            lines[index] = f'{CATALOG_SETTING} = "{value}"'
            return newline.join(lines) + newline
    lines.append(f'{CATALOG_SETTING} = "{value}"')
    return newline.join(lines) + newline


def backup_file(path: Path) -> Path:
    stamp = _datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = path.with_name(f"{path.name}.{stamp}.bak")
    shutil.copy2(path, target)
    return target


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the Codex model catalog from the active model provider and make "
            "config.toml reference it."
        )
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report what would change without writing any file",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        help=r"Codex state directory; defaults to CODEX_HOME or %USERPROFILE%\.codex",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"provider request timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    codex_home = (args.codex_home or default_codex_home()).resolve()
    config_path = codex_home / "config.toml"
    catalog_path = codex_home / "model_catalog.json"

    if not config_path.is_file():
        log(f"ERROR: config.toml was not found: {config_path}")
        return 1

    try:
        config = load_toml(config_path)
        provider_id, provider = resolve_provider(config)
        base_url = provider.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise CatalogError(f"model_providers.{provider_id} does not declare base_url")
        slugs = fetch_provider_models(
            base_url, provider_bearer_token(provider), args.timeout
        )
        previous_payload = load_catalog(catalog_path)
        desired = build_catalog(
            slugs,
            catalog_entries(previous_payload),
            extra_sources=(
                catalog_entries(load_catalog(codex_home / "models_cache.json")),
            ),
        )
    except CatalogError as exc:
        log(f"ERROR: {exc}")
        return 1

    config_text = read_text(config_path)
    catalog_ok = catalog_is_current(previous_payload, desired)
    setting_ok = setting_is_configured(config_text, catalog_path)

    log(f"Provider: {provider_id} ({base_url})")
    log(f"Provider models: {len(slugs)}")
    log(f"Catalog: {catalog_path}")
    log(f"Catalog up to date: {'yes' if catalog_ok else 'no'}")
    log(f"config.toml {CATALOG_SETTING}: {'yes' if setting_ok else 'no'}")

    if args.check_only:
        if not catalog_ok:
            for entry in desired["models"]:
                log(f"  would record: {entry['slug']} ({entry['visibility']})")
        if not setting_ok:
            log(f'  would set {CATALOG_SETTING} = "{catalog_path.as_posix()}"')
        return 0

    changed = False
    if not catalog_ok:
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(desired, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log(f"Wrote catalog with {len(desired['models'])} models")
        changed = True

    if not setting_ok:
        backup = backup_file(config_path)
        log(f"config.toml backup: {backup}")
        config_path.write_text(
            ensure_catalog_setting(config_text, catalog_path), encoding="utf-8"
        )
        log(f"Updated {CATALOG_SETTING} in config.toml")
        changed = True

    log("Model catalog is in sync." if changed else "Model catalog already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
