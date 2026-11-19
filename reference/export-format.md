# Kore.ai export internals (what kxo relies on)

Verified on an XO / AI for Service v11 export (`environmentVersionInfo` 9.1.7). Export = `appDefinition.json`
(one big JSON, 4-space indent) + `config.json` (env variables) + `icon.png`.

## Relevant top-level keys of appDefinition.json

| key | content |
|---|---|
| `defaultLanguage`, `supportedLanguages` | locale used for `localeData[lang]` |
| `dialogs[]` | dialog tasks: `_id` (dg-...), `refId`, `lname`, `localeData[lang].{name, shortDesc}`, `isHidden`, `nodes[]`, `sequences[]`, misc settings |
| `dialogComponents[]` | every node's component: `_id` (dc-...), `refId`, `name`, `type`, type-specific fields |
| `sentences[]` | training utterances: `{taskId: <intent component _id>, type: "DialogIntent", taskName, sentence, language}` |
| `patterns[]` | one entry per intent/entity component (`refId`, `taskName`, `taskType`) |
| `tools[]` | app-level agent tools: `refId`, `tool_name`, `params`, `tool_actions[{action_id: <component refId>, action_type}]`, `resp_path` |
| `exportOptions.subTasks.dialogs` | dialog `_id`s included in the export |
| `latestEncryptedHash`, `encryptedHash` | opaque; not reproducible client-side. kxo keeps them (`build --strip-hash` removes) |

## Identity

- Import matches dialogs and components by **refId** (Kore docs: same Reference ID replaces, new Reference ID
  creates). Export `_id`/`refId` are identical to the ones in the live app (published version ids).
- New objects get fresh uuid4 `refId`s and `dg-`/`dc-` ids.
- Component `name` is unique per app (case-insensitive) and is what scripts reference: `context.<Name>`.

## Dialog nodes

```json
{"type": "message", "componentId": "dc-...", "nodeId": "nd-msg-<uuid>",
 "transitions": [{"default": "nd-...", "metadata": {"color": "#D0D5DD", "connId": "dummy-<uuid>", "pathName": "", "pathsMap": []}}],
 "vNameSpace": [], "preConditions": [], "useTaskLevelNs": true, "nodeOptions": {...}}
```
- Entry node is always `nodeId: "intent0"` (type intent). Its first transition's `default` = start node.
- Conditional transitions: `{"if": {...}, "then": "<nodeId>"}`; `default` = else. Special targets: `end`, `retToFlow`, `""`.
- Agent nodes (`aiassist`) have `toolInstances[{nodeId: nd-tl-..., toolRefId, tool_transition:{type:0|1}, nodes:[{nodeId, componentId, type, nodeOptions}]}]`.
  `type 1` = exit the agent after the tool, `0` = back to the agent.
- `sequences[]` is canvas layout only (groups of nodes with x/y). kxo appends one sequence per new node.

## Encodings

- `script`, message `localeData[lang].text`, service `payload.value`, `sampleResponse[].response`: JS `encodeURIComponent`.
- service `endPoint.path`: JS `encodeURI` (keeps `/ ? & = :`).
- Agent `systemContext`/`rules`, prompt-node `prompt`, service `headers.value` (JSON string): plain.
- Some older components store plain text in encoded fields; Kore accepts both. kxo decodes on read and only
  re-encodes fields whose content changed.

## Component shapes (export)

- message / dialogAct / entity / form: `message[] = [{channel, localeData: {en: {text, type: basic|uxmap}}}]`,
  entity also `entityType`, `errorMessage[]`; dialogAct `localeData.en.synonyms`.
- script: `script`.
- service: `serviceType: rest`, `endPoint{path, method,...}`, `headers{type: raw, value}`, `payload{type: raw, value}`,
  `authRequired`, `idp`, `serviceAPITimeout`, `timeOutError`. Non-custom `serviceNodeType` services (system
  services) have no endPoint.
- generativeai (Prompt node): `generativeAI.settings` (model config), `generativeAI.prompt`.
- aiassist (Agent node): `generativeAI.dynamicEntityConfig{model..., systemContext, rules[], exitScenarios[], dynamicEntities[]}`.
- intent: `dialogId`, `localeData.en.{intent, desc}`.
