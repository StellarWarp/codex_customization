# Ultra Effort Investigation

## Scope

This note records the investigation of missing `ultra` in the Codex Desktop
model picker for the portable build. The Store package was inspected read-only;
the Store/MSIX installation was not modified.

## Evidence

- Store package inspected: Codex Desktop `26.810.7004.0`.
- The current webview bundle is `webview/assets/app-initial-TxV8Ik1J.js`.
- `model_catalog.json` and `models_cache.json` both declare `gpt-5.6-sol`
  with `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`.
- The native model-list path uses `supportedReasoningEfforts` and the GUI
  also contains a separate Work/Power picker path using
  `versionOptions`, `options`, `sliderSettings`, and `thinkingEffort`.

## Patch Experiments

The following local commits were diagnostic experiments and are intentionally
not retained after the rollback:

- `f5ef500`: local fallback for the Ultra picker setting.
- `4aa17c2`: connected the local setting to picker state; this initially used
  an invalid configuration descriptor as an atom and caused Desktop startup
  errors.
- `f6e1d45`: removed the invalid atom access and kept the Statsig gate bypass.
- `e22572f`: short-circuited model effort filtering, enabled-effort filtering,
  Statsig include parameters, static Power candidates, and `thinkingEffort`
  filtering.
- `51d019d`: additionally bypassed the legacy Work picker initialization gate
  that depends on ChatGPT user-settings data.

All diagnostic replacements were equal-length ASAR substitutions. The real
Store bundle passed Node syntax checks and the repository test suite passed
11/11 tests. Despite this, Ultra did not appear in the user's GUI. This means
the remaining issue is likely in the newer Work/Power model option mapping or
runtime data shape rather than the already identified effort/Statsig filters.

## Decision

Ultra is not required for the current workflow. The repository is therefore
rolled back to `a7f52f85d79043e018e34dc21b8ba4795f9d9f05`, which retains the
model visibility matcher repair while removing the unsuccessful Ultra-specific
experiments.

