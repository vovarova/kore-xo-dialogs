# Kore.ai bot project (one bot)

This folder holds exactly **one** Kore.ai XO bot (see `kore.project.json`). Another bot = another folder.

```
KORE = {{KORE}}
```

Run `$KORE` from this folder (the one with `kore.project.json`).

## Environments and secrets (read first)

- Env-variable values and credentials live in `env/<name>.json`, one file per environment (e.g. `uat`, `prod`).
  Everywhere else they are placeholders such as `__KXOENV_BaseUrl__` / `__KXOSEC_apiKey_1__`.
- `env/` and `workspace/import-package/` (built zips) hold real values: git-ignored, skipped by search, blocked by
  permission rules and a hook. **Never read, search, list, copy or print them**, and no recursive shell searches over
  the project (use the Grep/Glob tools or name `dialogs/`, `specs/`, `workspace/`). If a value must be added or
  changed, ask the user to edit the env file.
- Keep placeholders as they are. In new or changed nodes reference env vars as `{{env.KEY}}` (messages, URLs,
  headers) or `env.KEY` (scripts). Never invent placeholders or paste values into specs.

## 1. Import

```
$KORE import "<export.zip>" --env uat --to <new folder>   # new project
$KORE import "<export.zip>" --env uat                      # refresh: overwrites the workspace (review with git diff)
```

Export in Kore: App Settings > App Management > Export (JSON). Another environment's values: import that
environment's export with its `--env` name, or the user copies an env file and edits the values.

## 2. Develop

Start with `README.md`, then `dialogs/<Dialog>/flow.md`; shared nodes in `components.md`, env keys in `env.md`, agent
tools in `tools.md`. All edits go through kxo specs (rules: the `kore-xo-dialogs` skill, `reference/spec-format.md`):

```
$KORE kxo show "<Dialog>"
$KORE kxo apply "dialogs/<Dialog>/spec.json"     # after editing it in place; new dialogs/partial specs go in specs/
$KORE kxo clone "<Dialog>" "<New Dialog>" --utterance "..."
$KORE kxo validate
git diff                                         # what changed (git is the baseline)
```

Never edit `workspace/` or the generated files (`README.md`, `dialogs/`, `env.md`, `tools.md`, `components.md`,
`index.json`) by hand; they are regenerated after every apply/clone.

## 3. Build

```
$KORE build --env uat      # -> workspace/import-package/<Bot>-uat.zip (values of env/uat.json, hash removed)
```

The user imports the zip in Kore (App Settings > App Management > Import >
Incremental Import). Deploying and testing are out of scope.
