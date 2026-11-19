# kxo dialog spec format

One JSON object per dialog (or a JSON array of them). `kxo get` produces exactly this format, so the
quickest way to learn it is `kxo get -w ws "<Dialog>"`.

```jsonc
{
  "dialog": "Book Flight",          // display name; identifies the dialog (or use "refId")
  "refId": "f32b2811-...",          // from `get`; most reliable identity. Omit for new dialogs
  "rename": "Book a Flight",        // optional: change the display name
  "description": "...",             // dialog description (DialogGPT uses it for intent matching)
  "hidden": false,
  "intent": {
    "name": "Book_Flight",          // intent component name; only used when creating
    "utterances": ["book a flight"] // replaces the training utterances (default language) when given
  },
  "start": "SearchFlightAgent",     // first node after the intent
  "nodes": [ ... ],                 // nodes to create or update; nodes NOT listed are left untouched
  "remove": ["OldNode"],            // nodes to remove from this dialog (component stays in the app)
  "appTools": [ ... ]               // optional app-level tools for agent nodes (see below)
}
```

## Matching (create vs update)

A spec node updates an existing node when it has an `"id"` (nodeId, from `get`) or its `"name"` matches a node
already in the dialog. Otherwise a new node + component is created (needs `name` and `type`).
Only the fields you include are changed; everything else on the node/component is preserved, so a
partial spec is fine:

```json
{"dialog": "Book Flight", "nodes": [{"name": "Confirmation0003", "text": "Shall I book it?"}]}
```

`"use": "ExistingComponent"` always adds a new node that reuses a component that already exists elsewhere in
the app (the component becomes shared).

`"key": "anyLabel"` gives a node a local alias that transitions/`start` can target. Useful when the same
component appears twice in one dialog (names are then ambiguous) or for generated specs.

Shared components (used by other dialogs) can be listed and re-applied freely; kxo only refuses when the
spec would actually *change* them (add `"fork": true` or pass `--allow-shared`).

## Node types and fields

| `type` (Kore type)              | fields                                                                                  |
|---------------------------------|-----------------------------------------------------------------------------------------|
| `message`                       | `text` or `prompts`                                                                     |
| `entity`                        | `entityType` (string, number, date, city, phone_number, email, label, ...), `text`/`prompts`, `errorPrompts` |
| `confirmation` (dialogAct)      | `text`/`prompts`, `synonyms` `{"yes":[...],"no":[...]}`, shortcut transitions `yes` / `no` |
| `script`                        | `script` (JavaScript, plain text)                                                       |
| `service`                       | `method`, `url`, `headers` (object), `body` (string or JSON object), `timeout` (s)     |
| `prompt` (generativeai)         | `prompt`, `llmTemplate` (node to copy the model config from, on create)                 |
| `agent` (aiassist)              | `systemContext`, `rules` [..], `exitScenarios` [..], `entities` [{name,type}], `tools`, `llmTemplate` |
| `logic`, `agentTransfer`, `searchai`, `dynamicIntent` (`intentPath`), `form`, `webhook` | transitions + `raw` |

`prompts` = list of `{"text": "...", "type": "basic"|"uxmap", "channel": "default"}`; `type: "uxmap"` means the
text is JavaScript that calls `print(...)` (Kore "JavaScript" message). `text: "..."` is shorthand for one basic
default-channel prompt. Messages can use Kore templates like `{{context.x}}`, `{{env.API}}`, `{{content.KEY}}`.

Read-only info that `get` emits and `apply` ignores: `model`, `shared` (other dialogs using this node).

Escape hatches for anything kxo does not model (IVR settings, rephrase, sample responses, auth...):
- `"raw": {...}` deep-merged into the component JSON (see `kxo get --raw` for the real structure;
  remember Kore stores scripts/messages URL-encoded inside raw JSON).
- `"nodeRaw": {...}` deep-merged into the dialog node (e.g. `nodeOptions`).

`"fork": true` (or `"fork": "NewName"`) on a shared node: copy the component for this dialog before editing.

## Transitions

Pick one form per node:

```jsonc
"next": "NodeName"                                // always go to NodeName ("end" = end of dialog, "" = unconnected)
"yes": "A", "no": "B"                              // confirmation nodes; patches only the branch(es) given
"transitions": [                                   // ordered; first matching condition wins
  {"if": {"context": "context.isBusiness", "op": "eq", "value": "true"}, "to": "A"},
  {"if": {"field": "MyEntity", "op": "lt", "value": "6"}, "to": "B"},     // entity value (entity nodes)
  {"if": {"conjoin": "or", "tests": [
      {"context": "context.channel", "op": "eq", "value": "audiocodes"},
      {"context": "context.channel", "op": "eq", "value": "korevg"}]}, "to": "C"},
  {"if": {"dialogAct": "yes"}, "to": "D"},                                 // confirmation outcome
  {"default": "E"}                                                         // else
]
```

`next` and `transitions` replace the node's whole transition list (in order).
Targets are node names, nodeIds (used by `get` when a name appears twice in the dialog), `key`s, `"end"`,
`"retToFlow"`, or `""`.
The `if` object is passed to Kore as-is. Operators seen in real exports: `eq`, `lt`; Kore's UI also offers
others (e.g. `ne`, `gt`, `gte`, `lte`, `exists`, `contains`) — check one in the builder after import if unsure.
Extra transition keys (`userUtterance`, `isDeflectToChat`, `deflectToChat`) are preserved / settable.

## Agent node tools

```jsonc
"tools": [
  {"tool": "SearchFlights", "after": "agent", "actions": ["SearchFlightsService"]},
  {"tool": "SelectFlight",  "after": "exit",  "actions": [{"name": "SelectFlightScript", "script": "..."}]}
]
```
`tool` = name of an app-level tool, `after`: `agent` (return to the agent) or `exit` (leave the agent node via
its transition); kept as-is when omitted. `actions` = nodes run when the tool is called (replaces that tool's
action list on this agent): names of existing components, or full node specs to create/patch. A newly created
action is also registered on the app-level tool, which affects every agent using that tool. Tools not listed
stay on the agent; `{"tool": "X", "remove": true}` detaches one. New app-level tools:

```jsonc
"appTools": [{"name": "GetFare", "desc": "Fare lookup", "respPath": "context.GetFare",
              "params": [{"name": "fareClass", "desc": "economy or business", "type": "String"}],
              "actions": ["LookupFare"]}]   // actions must already exist as components
```
