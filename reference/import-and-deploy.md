# Getting files in and out of Kore

## Export (UI)
App Settings > App Management > Export. Choose JSON export, latest (in-development) version, include the
dialog tasks you care about. Unzip if it downloads a zip (kxo also accepts the zip directly).

## Import (UI) - recommended
1. Back up first: export the target app (Full Import cannot be undone).
2. App Settings > App Management > Import > **Incremental Import**.
3. Upload `build/appDefinition.json` (App Definition) and `build/config.json` (App Config), icon optional.
4. Select **Dialog tasks** and only the dialogs `kxo build` listed, plus **NLP Data / utterances**
   (otherwise new utterances are not imported).
5. After import: open a changed dialog in the builder, run Test, then publish as usual.

Behaviour (Kore docs, App Management): incremental import replaces tasks with the same Reference ID and resets
them to *Configured*, creates tasks with new Reference IDs, leaves target-only tasks untouched. It does not
delete anything. Full Import replaces the whole in-development app with the file.

## Public API (BETA, optional)
Needs an app created in **Admin Console > API Scopes** with *Bot Definition > Bot Export / Bot Import* scope.

```
export KORE_HOST=https://<your-platform-host>      # e.g. https://de-platform.kore.ai
export KORE_BOT_ID=st-...                          # App ID / Stream ID (App Settings > General)
export KORE_CLIENT_ID=cs-...  KORE_CLIENT_SECRET=...
python kxo.py export-api -o export.zip            # POST /api/public/bot/{id}/export + poll status
python kxo.py deploy -w ws --dry-run              # rebuilds, shows the import request, uploads nothing
python kxo.py deploy -w ws                        # rebuild + upload files + POST /api/public/bot/{id}/import + poll
```
JWT: HS256, payload `{"sub": "...", "appId": <client id>}`, sent as header `auth`.
Endpoints used: `/api/public/uploadfile` (fileContext=bulkImport), `/api/public/bot/{id}/import`,
`/api/public/bot/import/status/{bir}`, `/api/public/bot/{id}/export`, `/api/public/bot/{id}/export/status`.
The `importOptions.subTasks.dialogs` selection that `deploy` sends mirrors the export's own `exportOptions`
but is not documented by Kore: verify on a test app before relying on it.
