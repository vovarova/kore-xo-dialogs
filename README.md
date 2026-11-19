# kore-xo-dialogs

Claude Code skill + CLIs for working on Kore.ai XO / AI for Service bots as files: import an app export, edit
dialogs through JSON specs with Claude, and build an import zip per environment, instead of clicking through the
dialog builder.

## Install (either way)
- **Per user**: copy this folder to `~/.claude/skills/kore-xo-dialogs/`. Bot projects then call
  `python "$HOME/.claude/skills/kore-xo-dialogs/scripts/kore.py"`, so every teammate installs it the same way.
- **Per repo**: commit this folder as `<repo>/.claude/skills/kore-xo-dialogs/`. Bot folders inside that repo (the
  repo root itself, or subfolders like `bots/<bot>/`) call it by a relative path; nothing to install per user.
- Python 3.8+ on `PATH` as `python` (standard library only, no packages).

`kore import` writes the protection (hook + deny rules in `.claude/settings.json`) into the bot folder and, when the
bot is inside a git repo, also into the repo root, so `env/` stays blocked wherever Claude Code is started. Existing
`CLAUDE.md`, `.gitignore` and settings are merged, not replaced (kore keeps its part of `CLAUDE.md` between
`kore:begin`/`kore:end` markers).

## Bot projects (`scripts/kore.py`) - one folder per bot

```
python "$HOME/.claude/skills/kore-xo-dialogs/scripts/kore.py" import "<export.zip>" --env uat --to my-bot
cd my-bot
# ask Claude for changes; it edits dialogs through specs:  kore.py kxo apply specs/new.json
python "$HOME/.claude/skills/kore-xo-dialogs/scripts/kore.py" build --env uat
#   -> workspace/import-package/<Bot>-uat.zip : Kore > App Settings > App Management > Import > Incremental Import
```

- **Import** writes an anonymised workspace and AI-friendly views (`README.md`, `dialogs/<Dialog>/flow.md` +
  `spec.json`, `env.md`, `tools.md`, `components.md`) plus the project's `CLAUDE.md` and Claude settings.
- **Environments**: `env/<name>.json` holds one environment's env-variable values and credentials
  (`{"env": {...}, "secrets": {...}}`). Add one by importing that environment's export with `--env <name>` or by
  copying and editing a file. `kore build --env <name>` combines it with the workspace into the import zip
  (encryptedHash removed, which Kore requires for edited exports).
- **Secrets**: `env/` and `workspace/import-package/` are git-ignored and blocked for Claude (permission rules + a
  hook in `.claude/`). Everything else in the project is meant to be committed; use `git diff` to see changes.

## kxo only (`scripts/kxo.py`), without a project

```
python scripts/kxo.py init "<export folder or zip>" ws
python scripts/kxo.py list -w ws
python scripts/kxo.py get -w ws "Book Flight" -o book.json     # edit book.json
python scripts/kxo.py apply -w ws book.json
python scripts/kxo.py apply -w ws examples/new-dialog.json      # new dialog from a spec
python scripts/kxo.py clone -w ws "Book Flight" "Book Hotel" --utterance "book a hotel"
python scripts/kxo.py diff -w ws
python scripts/kxo.py build -w ws --strip-hash                  # then Incremental Import in Kore
```

Check kxo against your own export first: `python scripts/selftest.py "<export>"`.

Docs: `SKILL.md` (workflow and rules), `project/CLAUDE.md` (rules inside a bot project),
`reference/spec-format.md`, `reference/export-format.md`, `reference/import-and-deploy.md`.
