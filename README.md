# Codex Portable Tools

Windows scripts for building and launching a patched portable copy of Codex
Desktop while keeping the installed Microsoft Store/MSIX package unchanged.

The portable build enables Fast Mode.

Codex Desktop builds its model picker from the app-server `model/list` call.
For a custom `model_provider` that call only returns the ChatGPT account
catalog, so models served by a local OpenAI-compatible gateway never appear in
the picker. The tooling points the top-level `model_catalog_json` setting at a
catalog generated from the provider's `/v1/models` endpoint, which keeps the
custom models selectable while reading their metadata from the official catalog.

## Configuration

Shared defaults live in `codex_config.cmd`:

- `CODEX_PORTABLE_TARGET`: destination of the portable application.
- `CODEX_PROXY`: proxy injected by both launchers.
- `CODEX_PYTHON_EXE`: optional explicit Python interpreter.
- `CODEX_PACKAGE_ROOT`: optional Store package location fallback for restricted
  shells where `Get-AppxPackage` cannot see Codex.

The portable target defaults to `%USERPROFILE%\PortableApp\CodexPortable`.
Python is auto-detected from active Conda environments and common Miniconda or
Anaconda locations under both `%LOCALAPPDATA%` and `%USERPROFILE%`.

For settings that differ between computers, copy
`codex_config.local.example.cmd` to `codex_config.local.cmd` and edit the
local file. Git ignores the local file.

## Commands

- `update_codex_portable.cmd`: validate, rebuild, replace, refresh the model
  catalog, and launch the portable copy.
- `launch_codex_portable.cmd`: launch the custom portable copy through the
  configured proxy.
- `sync_codex_model_catalog.py`: rebuild
  `%USERPROFILE%\.codex\model_catalog.json` from the active provider and make
  `config.toml` reference it. Backs up `config.toml` before the first write.
- `launch_original_codex.cmd`: launch the registered original Codex package
  through the configured proxy.
- `install_codex_start_menu_shortcuts.cmd`: install `Codex Original` and
  `Codex Custom` shortcuts for the current Windows user.

Use `--check-only` with either launcher or the update script for read-only
validation. Remove the Start Menu shortcuts with:

```cmd
install_codex_start_menu_shortcuts.cmd --remove
```

## Portable launch and package identity

A portable copy that lives outside an MSIX package cannot start on its own: the
bootstrap calls `getCurrentPackageFamily()` and aborts with "the process has
no package identity". The portable launcher therefore starts the executable
through `Invoke-CommandInDesktopPackage` against the registered `OpenAI.Codex`
package, which borrows that identity without modifying the Store install.
Codex Desktop must be installed and registered for the portable copy to run.

Environment variables are not inherited by the packaged child, so
`HTTP_PROXY`/`HTTPS_PROXY` set by the launcher do not reach the app. The app
uses the Windows user proxy instead; the launcher checks that
`ProxyEnable`/`ProxyServer` match `CODEX_PROXY` and warns when they do not.
The portable copy shares Electron user data and the single-instance lock with the
official app, so close any running Codex Desktop before starting it.

## New Computer

1. Install Codex Desktop from the Microsoft Store.
2. Install Conda Python, or configure `CODEX_PYTHON_EXE`.
3. Clone this repository.
4. Create `codex_config.local.cmd` when the defaults do not fit the computer.
5. Run `update_codex_portable.cmd`.
6. Run `install_codex_start_menu_shortcuts.cmd`.
