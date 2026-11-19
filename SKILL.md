---
name: kore-xo-dialogs
description: Create new dialog tasks or modify existing ones in a Kore.ai XO / AI for Service app by editing its export (appDefinition.json) and re-importing it, instead of clicking through the dialog builder. Use when the user wants to add, change, rewire, clone or bulk-edit Kore.ai dialogs, nodes (message, entity, script, service, confirmation, agent node, prompt node, logic), transitions, utterances or agent-node prompts/rules, or mentions a Kore export folder / appDefinition.json.
---

# Kore.ai XO dialog kit (kxo)

Edits dialog tasks inside a Kore.ai app **export** with `scripts/kxo.py` (Python 3.8+, stdlib only),
then produces files for Kore's **Incremental Import**. Works for any app: it reads node templates,
LLM model configs and language from the export itself.

```
Kore: App Settings > App Management > Export (JSON)   ->  folder/zip with appDefinition.json + config.json
kxo init <export> <ws>        make a workspace (keeps a pristine copy for diff)
kxo list / show / get         inspect; `get` writes an editable JSON spec of one dialog
kxo apply <spec.json>         create or update dialogs from specs (validates, refuses unsafe edits)
kxo clone <dialog> <name>     new dialog copied from an existing one
kxo validate / diff           integrity check; what changed vs the original export
kxo build                     import-ready appDefinition.json + config.json + zip, and the list of dialogs to tick
Kore: App Management > Import > Incremental Import  (select those dialogs + NLP data)
```

`KXO = python <this skill dir>/scripts/kxo.py`. All commands take `-w <workspace>` (or env `KXO_WORKDIR`).

## Project pipeline (preferred): `kore`

`KORE = python <this skill dir>/scripts/kore.py`. **One project folder = one bot** (marker `kore.project.json`; its
`CLAUDE.md` has the rules). In such a folder use `kore`, not bare kxo.

1. **Import** `$KORE import <export.zip> --env <name> [--to <new folder>]`: anonymised workspace + views (`README.md`,
   `dialogs/<Dialog>/{flow.md,spec.json}`, `env.md`, `tools.md`, `components.md`, `index.json`); env values and
   credentials go to `env/<name>.json` (one file per environment), placeholders stay everywhere else.
2. **Develop** `$KORE kxo <show|get|apply|clone|validate ...>` (views regenerate after edits); changes: `git diff`.
3. **Build** `$KORE build --env <name>` -> `workspace/import-package/<Bot>-<name>.zip` with that env's values, hash
   removed. The user imports it in Kore; deploying/testing are out of scope.

Never read, search or list `env/` or `workspace/import-package/`; never write values or new placeholders into specs,
reference `{{env.KEY}}` / `env.KEY`. Values are edited by the user in the env files.

## How to work

1. **Get the export.** Ask for the path if not given. If the workspace does not exist yet: `$KXO init "<export>" <ws>`.
   Never edit the user's original export folder; the workspace is a copy.
2. **Look before editing.** `$KXO list -w ws` (add `--all` for hidden/system dialogs), `$KXO show -w ws "<Dialog>"`.
3. **Edit through specs, never by hand-editing appDefinition.json.**
   - Modify: `$KXO get -w ws "<Dialog>" -o spec.json`, edit only what is needed (or write a *partial* spec that
     lists just the dialog name and the nodes to change/add), then `$KXO apply -w ws spec.json`.
   - New dialog: write a spec (see `examples/new-dialog.json`), `apply` it.
   - Variant of an existing dialog: `$KXO clone -w ws "<Dialog>" "<New Name>" --utterance "..."`, then patch it.
   - Use `--dry-run` first for big changes. `apply` is idempotent: re-applying the same spec is a no-op.
4. `$KXO validate -w ws` then `$KXO diff -w ws`, show the user the diff summary.
5. `$KXO build -w ws` and give the user the printed import steps + dialog list. Tell them to export a backup
   of the app first if it matters.
6. Deploying and conversation testing are out of scope: the user imports the build in Kore (do not use the
   kxo `deploy` / `export-api` commands).

Spec format, node types, transition/condition syntax: **read `reference/spec-format.md` before writing a spec.**
Export file internals (ids, refIds, encodings, where utterances live): `reference/export-format.md`.

## Rules that prevent broken imports

- Node `name` = Kore component name: letters, digits, `_`, starting with a letter, **unique across the whole app**.
  To reuse an existing component in another dialog use `"use": "<ExistingName>"`.
- A node referenced by other dialogs is **shared**: kxo refuses to edit it unless the node has `"fork": true`
  (copy it for this dialog) or you pass `--allow-shared` (edit everywhere). Agent-node tool actions belong to
  app-level tools, so editing them always affects every agent using that tool. Explain this to the user.
- Renaming a node does not rewrite scripts/templates that reference it (`context.<Name>`,
  `context.GenerativeAINode.<Name>.text`, `context.AI_Assisted_Dialogs.<Agent>...`). `clone` gives nodes new
  names, so check those references in the copy and fix them with `apply`.
- Text values in specs are plain text; kxo handles Kore's URL-encoding. Only the bot's default language is
  edited; other locales keep their old text (kxo warns).
- New Prompt / Agent nodes copy the LLM model config from an existing node of the same type
  (`"llmTemplate": "<NodeName>"` to pick which). If the app has none, tell the user to choose the model in the UI.
- Incremental import never deletes dialogs or nodes in Kore. `delete-dialog`/`remove` only change the file;
  the user must also delete in the UI (or do a Full Import, which overwrites the whole app).
- Export files contain `encryptedHash` fields; kxo keeps them. If Kore rejects the edited file, rebuild with
  `build --strip-hash` and try again (behaviour is not documented by Kore).
- After import, open one changed dialog in the builder and run it in "Test" before publishing.

`scripts/selftest.py <export>` proves the kit on a given export (lossless round trip, create, partial edits,
shared-node guard, clone of every dialog, tools, delete, build) without touching the export. Run it once when
using a new app or Kore platform version; if it fails, stop and report rather than importing.
