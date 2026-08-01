# Codex Portable Tools

Windows scripts for building and launching a patched portable copy of Codex
Desktop while keeping the installed Microsoft Store/MSIX package unchanged.

The portable build enables Fast Mode and custom/hidden model visibility. An
experimental patch for the shipped `Settings > Connections > Control other
devices` workflow is available but disabled by default. It does not rebuild or
replace the Rust `resources\codex.exe` app server, whose remote-control
WebSocket currently does not honor the configured HTTP proxy.

## Configuration

Shared defaults live in `codex_config.cmd`:

- `CODEX_PORTABLE_TARGET`: destination of the portable application.
- `CODEX_PROXY`: proxy injected by both launchers.
- `CODEX_PYTHON_EXE`: optional explicit Python interpreter.
- `CODEX_ENABLE_REMOTE_CONTROL_PATCH`: set to `1` to enable the experimental
  remote-control UI and Windows device-key shim; defaults to `0`.

For settings that differ between computers, copy
`codex_config.local.example.cmd` to `codex_config.local.cmd` and edit the
local file. Git ignores the local file.

## Commands

- `update_codex_portable.cmd`: validate, rebuild, replace, and launch the
  portable copy.
- `launch_codex_portable.cmd`: launch the custom portable copy through the
  configured proxy.
- `launch_original_codex.cmd`: launch the registered original Codex package
  through the configured proxy.
- `install_codex_start_menu_shortcuts.cmd`: install `Codex Original` and
  `Codex Custom` shortcuts for the current Windows user.

Use `--check-only` with either launcher or the update script for read-only
validation. Remove the Start Menu shortcuts with:

```cmd
install_codex_start_menu_shortcuts.cmd --remove
```

When the optional patch is enabled, first enable MFA on the OpenAI/ChatGPT
account. Encrypted Windows device keys are stored in `%CODEX_HOME%\remote-control-device-keys.windows.json`,
or under `%USERPROFILE%\.codex` when `CODEX_HOME` is unset. The patch does not
make the native remote-control WebSocket use `CODEX_PROXY`; a TUN connection or
a separately rebuilt native app server is still required on affected releases.

## New Computer

1. Install Codex Desktop from the Microsoft Store.
2. Install Conda Python, or configure `CODEX_PYTHON_EXE`.
3. Clone this repository.
4. Create `codex_config.local.cmd` when the defaults do not fit the computer.
5. Run `update_codex_portable.cmd`.
6. Run `install_codex_start_menu_shortcuts.cmd`.
