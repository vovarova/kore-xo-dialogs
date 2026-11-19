#!/usr/bin/env python3
"""kxo - edit Kore.ai XO (AI for Service) dialog tasks inside an app export.

Workflow:  export app in Kore  ->  kxo init  ->  kxo get / apply / clone  ->  kxo validate
           ->  kxo build  ->  Incremental Import in Kore (select the changed dialogs).

Python 3.8+, standard library only.  Run `python kxo.py -h` for commands.
"""
import argparse
import base64
import copy
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

APPDEF = "appDefinition.json"
CONFIG = "config.json"
STATE_DIR = ".kxo"
ORIGINAL = "original_appDefinition.json"

# friendly type  <->  Kore component type
TYPE_ALIASES = {
    "message": "message", "entity": "entity", "script": "script", "service": "service",
    "confirmation": "dialogAct", "dialogact": "dialogAct",
    "agent": "aiassist", "aiassist": "aiassist",
    "prompt": "generativeai", "generativeai": "generativeai",
    "logic": "logic", "agenttransfer": "agentTransfer",
    "searchai": "searchai", "dynamicintent": "dynamicIntent",
    "form": "form", "webhook": "SDKWebHook", "sdkwebhook": "SDKWebHook",
}
FRIENDLY = {"dialogAct": "confirmation", "aiassist": "agent", "generativeai": "prompt",
            "agentTransfer": "agentTransfer", "SDKWebHook": "webhook", "dynamicIntent": "dynamicIntent"}
NODE_PREFIX = {"message": "msg", "entity": "ent", "script": "scr", "service": "srv", "dialogAct": "dac",
               "aiassist": "ais", "generativeai": "gai", "logic": "lgc", "agentTransfer": "agt",
               "searchai": "sai", "dynamicIntent": "din", "form": "frm", "SDKWebHook": "whk"}
TRANSITION_EXTRAS = ("userUtterance", "isDeflectToChat", "deflectToChat")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
SPECIAL_TARGETS = {"end", "retToFlow"}  # end of dialog / return to calling flow


class KxoError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def enc(s):
    """Kore stores scripts/messages/urls URL-encoded exactly like JS encodeURIComponent."""
    return urllib.parse.quote(s, safe="-_.!~*'()")


def enc_uri(s):
    """Service URLs are stored like JS encodeURI (keeps / ? & = : etc.)."""
    return urllib.parse.quote(s, safe="-_.!~*'();/?:@&=+$,#")


def dec(s):
    if not isinstance(s, str):
        return s
    try:
        return urllib.parse.unquote(s, errors="strict")
    except UnicodeDecodeError:
        return s


def new_uuid():
    return str(uuid.uuid4())


def new_object_id():
    return uuid.uuid4().hex[:24]


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data, indent=4):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def info(msg):
    print(msg)


def warn(msg):
    print("WARNING: " + msg, file=sys.stderr)


# --------------------------------------------------------------------------- app model
class App:
    def __init__(self, workdir):
        self.workdir = os.path.abspath(workdir)
        self.path = os.path.join(self.workdir, APPDEF)
        if not os.path.isfile(self.path):
            raise KxoError(f"{self.path} not found. Run `kxo init <export> {workdir}` first.")
        self.d = load_json(self.path)
        self.lang = self.d.get("defaultLanguage") or "en"
        for key in ("dialogs", "dialogComponents", "sentences", "patterns", "tools"):
            self.d.setdefault(key, [])
        self.reindex()

    def reindex(self):
        self.comp_by_id = {c["_id"]: c for c in self.d["dialogComponents"]}
        self.comp_by_lname = {}
        for c in self.d["dialogComponents"]:
            self.comp_by_lname.setdefault(c.get("name", "").lower(), c)
        self.tool_by_ref = {t["refId"]: t for t in self.d["tools"]}
        self.tool_by_name = {t.get("tool_name", "").lower(): t for t in self.d["tools"]}
        self.comp_by_ref = {c.get("refId"): c for c in self.d["dialogComponents"]}

    def save(self):
        save_json(self.path, self.d)

    # ---- dialogs
    def dname(self, dlg):
        ld = dlg.get("localeData") or {}
        return (ld.get(self.lang) or next(iter(ld.values()), {}) or {}).get("name") or dlg.get("name") or dlg.get("lname") or dlg["_id"]

    def ddesc(self, dlg):
        ld = dlg.get("localeData") or {}
        return (ld.get(self.lang) or {}).get("shortDesc", "")

    def find_dialog(self, key, required=True):
        key_l = key.lower()
        hits = [x for x in self.d["dialogs"]
                if x["_id"] == key or x.get("refId") == key or self.dname(x).lower() == key_l]
        if len(hits) > 1:
            raise KxoError(f"Dialog '{key}' is ambiguous; use its _id or refId: " + ", ".join(x["_id"] for x in hits))
        if not hits and required:
            raise KxoError(f"Dialog '{key}' not found. Use `kxo list`.")
        return hits[0] if hits else None

    def intent_comp(self, dlg):
        n = next((x for x in dlg["nodes"] if x.get("nodeId") == "intent0"), None) or \
            next((x for x in dlg["nodes"] if x.get("type") == "intent"), None)
        return self.comp_by_id.get(n["componentId"]) if n else None

    def utterances(self, intent_comp):
        if not intent_comp:
            return []
        return [s["sentence"] for s in self.d["sentences"]
                if s.get("taskId") == intent_comp["_id"] and s.get("language", self.lang) == self.lang]

    def component_usage(self):
        """componentId -> set of dialog names using it (incl. tool action nodes)."""
        use = {}
        for dlg in self.d["dialogs"]:
            nm = self.dname(dlg)
            for n in iter_all_nodes(dlg):
                use.setdefault(n["componentId"], set()).add(nm)
        return use

    def name_taken(self, name):
        return name.lower() in self.comp_by_lname

    def unique_name(self, base):
        base = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "Node"
        if not base[0].isalpha():
            base = "N_" + base
        name, i = base, 2
        while self.name_taken(name):
            name = f"{base}_{i}"
            i += 1
        return name

    # ---- templates copied from the app itself, so new things look like the platform's own
    def template_component(self, ctype, prefer=None):
        if prefer:
            c = self.comp_by_lname.get(prefer.lower())
            if not c:
                raise KxoError(f"template component '{prefer}' not found")
            return c
        cands = [c for c in self.d["dialogComponents"] if c.get("type") == ctype]
        # prefer non-system components (system ones are often namespaced / multi-locale)
        cands.sort(key=lambda c: (bool(c.get("vNameSpace")), len(c.get("localeData") or {}) > 1))
        return cands[0] if cands else None

    def template_node_options(self, ctype):
        for dlg in self.d["dialogs"]:
            for n in dlg["nodes"]:
                if n.get("type") == ctype and n.get("nodeOptions"):
                    no = copy.deepcopy(n["nodeOptions"])
                    no["message"], no["errorMessage"] = [], []
                    no.get("customTags", {}).update({"session": [], "message": [], "user": [], "altText": []})
                    return no
        return {"agentSkills": {"voice": [], "chat": []}, "transitionType": "auto", "isRetriesExceeded": False,
                "promptOptions": "required", "reuseMarkedupPhrases": False, "message": [], "errorMessage": [],
                "interruptOptions": {"priority": "task"}, "transitionMode": "initiateCurrentTask",
                "inputHandlingOptions": "executeIntent" if ctype == "aiassist" else "useAsEntityValue",
                "reuseOptions": "1", "userInputCorrection": True, "noAutoCorrection": False, "notForReuse": False,
                "addIntentToFollowUpIntents": True, "contextTags": [],
                "customTags": {"session": [], "message": [], "user": [], "altText": []},
                "containmentSettings": {"enabled": False}, "conversationSummary": {"enabled": False}}

    def template_dialog(self):
        user = [x for x in self.d["dialogs"] if not x.get("isHidden") and not x.get("vNameSpace")]
        src = (user or self.d["dialogs"] or [None])[0]
        if not src:
            return {"groups": [], "isFollowUp": False, "isAbandonment": False, "interruptOptions": {"priority": "bot"},
                    "contextLifeTime": {"options": "close"}, "amendConfig": {"priority": "bot"}, "vNameSpace": [],
                    "metaInfo": {"bookmarks": [], "conversations": []}, "dv": "v3"}
        keep = ("groups", "isFollowUp", "isAbandonment", "interruptOptions", "contextLifeTime", "amendConfig",
                "metaInfo", "dv", "agentAssistUseCase")
        t = {k: copy.deepcopy(src[k]) for k in keep if k in src}
        t["vNameSpace"] = []
        t["metaInfo"] = {"bookmarks": [], "conversations": []}
        return t


def iter_all_nodes(dlg):
    for n in dlg.get("nodes", []):
        yield n
        for ti in n.get("toolInstances") or []:
            for tn in ti.get("nodes") or []:
                yield tn


def base_component(app, name, ctype):
    return {"_id": "dc-" + new_uuid(), "refId": new_uuid(), "name": name, "piiDataEnabled": False,
            "serviceNodeType": "custom", "type": ctype, "applyDefaultTemplate": False,
            "localeData": {app.lang: {"label": name}}, "entityFields": {"cityAdvanced": []},
            "spellcorrectThreshold": 0, "preConditions": [], "vNameSpace": [], "useTaskLevelNs": True,
            "isSensitive": False, "isTransient": False, "skipDGPTStrictly": False}


def new_transition(**kw):
    t = dict(kw)
    t["metadata"] = {"color": "#D0D5DD", "connId": "dummy-" + new_uuid(), "pathName": "", "pathsMap": []}
    return t


# --------------------------------------------------------------------------- component <-> friendly fields
def read_prompts(app, comp, field):
    out = []
    for m in comp.get(field) or []:
        ld = m.get("localeData")
        loc = (ld.get(app.lang) if ld else m) or {}
        if "text" not in loc:
            continue
        p = {"text": dec(loc.get("text", ""))}
        if loc.get("type", "basic") != "basic":
            p["type"] = loc["type"]
        if m.get("channel", "default") != "default":
            p["channel"] = m["channel"]
        out.append(p)
    return out


def write_prompts(app, comp, field, prompts):
    if isinstance(prompts, str):
        prompts = [{"text": prompts}]
    old = comp.get(field) or []
    if read_prompts(app, comp, field) == [_norm_prompt(p) for p in prompts]:
        return False
    multi = [m for m in old if len((m.get("localeData") or {})) > 1]
    if multi:
        warn(f"{comp['name']}.{field}: has {len(multi[0]['localeData'])} languages; only '{app.lang}' is updated, "
             "other languages keep their old text.")
    new = []
    for i, p in enumerate(prompts):
        p = _norm_prompt(p)
        ch = p.get("channel", "default")
        base = copy.deepcopy(old[i]) if i < len(old) and old[i].get("channel", "default") == ch else {"channel": ch}
        base.setdefault("localeData", {})
        base["localeData"][app.lang] = {"text": enc(p["text"]), "type": p.get("type", "basic")}
        new.append(base)
    comp[field] = new
    return True


def _norm_prompt(p):
    if isinstance(p, str):
        return {"text": p}
    q = {"text": p.get("text", "")}
    if p.get("type", "basic") != "basic":
        q["type"] = p["type"]
    if p.get("channel", "default") != "default":
        q["channel"] = p["channel"]
    return q


def headers_to_dict(h):
    if not h:
        return {}
    v = h.get("value") if isinstance(h, dict) else h
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except (TypeError, ValueError):
        return {"__raw__": v}


def comp_to_fields(app, comp):
    """Friendly, decoded, editable view of a component (only the fields kxo knows)."""
    t = comp.get("type")
    f = {}
    if t in ("message", "entity", "dialogAct", "form"):
        f["prompts"] = read_prompts(app, comp, "message")
    if t == "entity":
        f["entityType"] = comp.get("entityType")
        f["errorPrompts"] = read_prompts(app, comp, "errorMessage")
    if t == "dialogAct":
        syn = ((comp.get("localeData") or {}).get(app.lang) or {}).get("synonyms")
        if syn:
            f["synonyms"] = syn
    if t == "script":
        f["script"] = dec(comp.get("script", ""))
    if t == "service":
        if comp.get("endPoint"):
            ep = comp["endPoint"]
            f["method"] = ep.get("method")
            f["url"] = dec(ep.get("path", ""))
        if comp.get("headers"):
            f["headers"] = headers_to_dict(comp.get("headers"))
        body = (comp.get("payload") or {}).get("value")
        if body not in (None, "undefined", ""):
            f["body"] = dec(body)
        f["timeout"] = comp.get("serviceAPITimeout")
        if comp.get("serviceNodeType", "custom") != "custom":
            f["serviceNodeType"] = comp["serviceNodeType"]
    if t == "generativeai":
        g = comp.get("generativeAI") or {}
        f["prompt"] = g.get("prompt", "")
        s = g.get("settings") or {}
        f["model"] = s.get("displayName") or s.get("model")
    if t == "aiassist":
        g = (comp.get("generativeAI") or {}).get("dynamicEntityConfig") or {}
        f["systemContext"] = g.get("systemContext", "")
        f["rules"] = g.get("rules", [])
        f["exitScenarios"] = g.get("exitScenarios", [])
        f["entities"] = g.get("dynamicEntities", [])
        f["model"] = g.get("displayName") or g.get("model")
    if t == "dynamicIntent":
        f["intentPath"] = comp.get("intentPath")
    return {k: v for k, v in f.items() if v is not None}


RAW_PROTECTED = {"_id", "refId", "name", "type"}  # identity: never changed through "raw"


def apply_fields(app, comp, spec, created):
    """Patch a component with the friendly fields present in `spec`. Returns True if changed."""
    t = comp["type"]
    before = json.dumps(comp, sort_keys=True)
    if spec.get("raw"):  # first, so explicit friendly fields below win over stale raw values
        deep_merge(comp, {k: v for k, v in spec["raw"].items() if k not in RAW_PROTECTED})
    if "text" in spec and "prompts" not in spec:
        spec = dict(spec, prompts=[{"text": spec["text"]}])
    if "prompts" in spec:
        if t not in ("message", "entity", "dialogAct", "form"):
            raise KxoError(f"{comp['name']}: 'prompts'/'text' not valid for type {t}")
        write_prompts(app, comp, "message", spec["prompts"])
    if "errorPrompts" in spec:
        write_prompts(app, comp, "errorMessage", spec["errorPrompts"])
    if "entityType" in spec and comp.get("entityType") != spec["entityType"]:
        comp["entityType"] = spec["entityType"]
    if "synonyms" in spec and ((comp.get("localeData") or {}).get(app.lang) or {}).get("synonyms") != spec["synonyms"]:
        comp.setdefault("localeData", {}).setdefault(app.lang, {})["synonyms"] = spec["synonyms"]
    if "script" in spec:
        if t != "script":
            raise KxoError(f"{comp['name']}: 'script' only valid for script nodes")
        if dec(comp.get("script", "")) != spec["script"]:
            comp["script"] = enc(spec["script"])
    if t == "service":
        if created or "method" in spec or "url" in spec:
            ep = comp.setdefault("endPoint", {"host": "", "port": "", "path": "", "protocol": "", "method": "get",
                                              "connectorEnabled": False, "piiDataEnabled": False})
            if "method" in spec and ep.get("method") != spec["method"].lower():
                ep["method"] = spec["method"].lower()
            if "url" in spec and dec(ep.get("path", "")) != spec["url"]:
                ep["path"] = enc_uri(spec["url"])
        if "headers" in spec and headers_to_dict(comp.get("headers")) != (spec["headers"] or {}):
            comp["headers"] = {"type": "raw", "value": json.dumps(spec["headers"], separators=(",", ":"))}
        if "body" in spec:
            cur = (comp.get("payload") or {}).get("value")
            cur = None if cur in (None, "undefined", "") else dec(cur)
            body = spec["body"]
            if body is not None and not isinstance(body, str):
                body = json.dumps(body, indent=2)
            if cur != body and not (body is None and "payload" not in comp):
                comp["payload"] = {"type": "raw", "value": "undefined" if body is None else enc(body)}
        if "timeout" in spec and comp.get("serviceAPITimeout") != spec["timeout"]:
            comp["serviceAPITimeout"] = spec["timeout"]
        if created:
            comp.setdefault("authRequired", False)
            comp.setdefault("payload", {"type": "raw", "value": "undefined"})
            comp.setdefault("headers", {"type": "raw", "value": "{}"})
            comp.setdefault("serviceType", "rest")
            comp.setdefault("idp", "none")
            comp.setdefault("timeOutError", {"closeConversation": True})
            comp.setdefault("serviceAPITimeout", 20)
            comp.setdefault("isClientCertEnabled", True)
    if "prompt" in spec:
        if t != "generativeai":
            raise KxoError(f"{comp['name']}: 'prompt' only valid for prompt (generativeai) nodes")
        if (comp.get("generativeAI") or {}).get("prompt", "") != spec["prompt"]:
            comp.setdefault("generativeAI", {})["prompt"] = spec["prompt"]
    if t == "aiassist" and any(k in spec for k in ("systemContext", "rules", "exitScenarios", "entities")):
        g = comp.setdefault("generativeAI", {}).setdefault("dynamicEntityConfig", {})
        for k, kk, empty in (("systemContext", "systemContext", ""), ("rules", "rules", []),
                             ("exitScenarios", "exitScenarios", []), ("entities", "dynamicEntities", [])):
            if k in spec and g.get(kk, empty) != spec[k]:
                g[kk] = spec[k]
    if "intentPath" in spec and comp.get("intentPath") != spec["intentPath"]:
        comp["intentPath"] = spec["intentPath"]
    return json.dumps(comp, sort_keys=True) != before


def init_llm_config(app, comp, spec):
    """New prompt/agent nodes copy their LLM configuration from an existing node of the same type."""
    t = comp["type"]
    src = app.template_component(t, spec.get("llmTemplate"))
    if t == "generativeai":
        if src:
            comp["generativeAI"] = {"settings": copy.deepcopy((src.get("generativeAI") or {}).get("settings", {})),
                                    "prompt": ""}
        else:
            comp["generativeAI"] = {"settings": {"enabled": True, "isSysIntAndCustomPrompt": False}, "prompt": ""}
            warn(f"{comp['name']}: no existing Prompt node to copy an LLM model from; pick the model in the UI.")
    elif t == "aiassist":
        if src:
            g = copy.deepcopy((src.get("generativeAI") or {}).get("dynamicEntityConfig", {}))
            for k in ("systemContext", "AdditionalInstructions", "system_context"):
                if k in g:
                    g[k] = ""
            g.update({"rules": [], "exitScenarios": [], "dynamicEntities": []})
            comp["generativeAI"] = {"dynamicEntityConfig": g}
        else:
            comp["generativeAI"] = {"dynamicEntityConfig": {"enabled": True, "rules": [], "exitScenarios": [],
                                                            "dynamicEntities": [], "systemContext": ""}}
            warn(f"{comp['name']}: no existing Agent node to copy an LLM model from; pick the model in the UI.")
    elif t in ("message", "dialogAct", "entity") and src and (src.get("generativeAI") or {}).get("rephrase"):
        comp["generativeAI"] = {"rephrase": copy.deepcopy(src["generativeAI"]["rephrase"])}


# --------------------------------------------------------------------------- dialog -> spec
def node_label_map(app, dlg):
    """nodeId -> human reference (component name, or nodeId when the name is not unique)."""
    names = {}
    for n in dlg["nodes"]:
        c = app.comp_by_id.get(n["componentId"])
        names[n["nodeId"]] = c["name"] if c else n["nodeId"]
    counts = {}
    for v in names.values():
        counts[v.lower()] = counts.get(v.lower(), 0) + 1
    return {k: (v if counts[v.lower()] == 1 else k) for k, v in names.items()}


def transitions_to_spec(trans, label):
    def ref(x):
        if x in (None, ""):
            return ""
        return label.get(x, x)

    out = []
    for t in trans or []:
        if "default" in t:
            e = {"default": ref(t["default"])}
        else:
            e = {"if": t.get("if", {}), "to": ref(t.get("then"))}
        for k in TRANSITION_EXTRAS:
            if k in t:
                e[k] = t[k]
        out.append(e)
    return out


def dialog_to_spec(app, dlg, raw=False):
    label = node_label_map(app, dlg)
    usage = app.component_usage()
    me = app.dname(dlg)
    ic = app.intent_comp(dlg)
    spec = {"dialog": me, "refId": dlg.get("refId"), "description": app.ddesc(dlg),
            "hidden": bool(dlg.get("isHidden")),
            "intent": {"name": ic["name"] if ic else None, "utterances": app.utterances(ic)}}
    nodes = []
    for n in dlg["nodes"]:
        if n["nodeId"] == "intent0":
            d = (n.get("transitions") or [{}])[0].get("default", "")
            spec["start"] = label.get(d, d)
            continue
        c = app.comp_by_id.get(n["componentId"])
        if not c:
            nodes.append({"id": n["nodeId"], "type": n["type"], "error": "missing component " + n["componentId"]})
            continue
        e = {"id": n["nodeId"], "name": c["name"], "type": FRIENDLY.get(c["type"], c["type"])}
        others = sorted(usage.get(c["_id"], set()) - {me})
        if others:
            e["shared"] = others
        e.update(comp_to_fields(app, c))
        if n.get("toolInstances"):
            e["tools"] = tools_to_spec(app, n)
        tr = transitions_to_spec(n.get("transitions"), label)
        if len(tr) == 1 and set(tr[0]) == {"default"}:
            e["next"] = tr[0]["default"]
        else:
            e["transitions"] = tr
        if raw:
            e["raw"] = decode_raw(c)
        nodes.append(e)
    spec["nodes"] = nodes
    return spec


def tools_to_spec(app, node):
    out = []
    for ti in node.get("toolInstances") or []:
        tool = app.tool_by_ref.get(ti.get("toolRefId"))
        acts = []
        for tn in ti.get("nodes") or []:
            c = app.comp_by_id.get(tn["componentId"])
            if c:
                a = {"name": c["name"], "type": FRIENDLY.get(c["type"], c["type"])}
                a.update(comp_to_fields(app, c))
                acts.append(a)
        out.append({"tool": tool["tool_name"] if tool else ti.get("toolRefId"),
                    "after": "exit" if (ti.get("tool_transition") or {}).get("type") == 1 else "agent",
                    "actions": acts})
    return out


def decode_raw(c):
    c = copy.deepcopy(c)
    for k in RAW_PROTECTED:
        c.pop(k, None)
    return c


# --------------------------------------------------------------------------- spec -> dialog
class Applier:
    def __init__(self, app, spec, allow_shared=False):
        self.app, self.spec, self.allow_shared = app, spec, allow_shared
        self.log = []
        self.changed_comps = set()

    def say(self, m):
        self.log.append(m)

    def run(self):
        app, spec = self.app, self.spec
        if not spec.get("dialog"):
            raise KxoError("spec needs a 'dialog' name")
        for t in spec.get("appTools") or []:
            self.upsert_app_tool(t)
        dlg = None
        if spec.get("refId"):
            dlg = next((x for x in app.d["dialogs"] if x.get("refId") == spec["refId"]), None)
        if not dlg:
            dlg = app.find_dialog(spec["dialog"], required=False)
        created = dlg is None
        if created:
            dlg = self.create_dialog()
        else:
            self.update_dialog_meta(dlg)
        self.dlg = dlg
        self.me = app.dname(dlg)
        self.usage = app.component_usage()  # componentId -> dialog names; other dialogs don't change here
        self.label_to_id = {}
        for n in dlg["nodes"]:
            c = app.comp_by_id.get(n["componentId"])
            if c:
                self.label_to_id.setdefault(c["name"].lower(), n["nodeId"])
            self.label_to_id[n["nodeId"].lower()] = n["nodeId"]

        # pass 1: ensure every spec node exists (so transitions can point forward)
        node_specs = spec.get("nodes") or []
        resolved = []
        for ns in node_specs:
            resolved.append((ns, self.ensure_node(ns)))
        # pass 2: transitions + tools
        for ns, node in resolved:
            self.apply_transitions(node, ns)
            if "tools" in ns:
                self.apply_tools(node, ns["tools"])
        if "start" in spec:
            intent0 = next(n for n in dlg["nodes"] if n["nodeId"] == "intent0")
            target = self.resolve(spec["start"])
            trans = intent0.setdefault("transitions", [])
            if not trans:
                trans.append(new_transition(default=target))
                self.say(f"start -> {spec['start']}")
            elif trans[0].get("default") != target:
                trans[0] = dict(trans[0], default=target)  # other entry transitions are kept
                self.say(f"start -> {spec['start']}")
        for name in spec.get("remove") or []:
            self.remove_node(name)
        self.sync_sequences()
        if created:
            self.say(f"created dialog '{spec['dialog']}' ({dlg['_id']})")
        return dlg, created

    # ---- dialog level
    def create_dialog(self):
        app, spec = self.app, self.spec
        name = spec["dialog"]
        if app.find_dialog(name, required=False):
            raise KxoError(f"dialog '{name}' already exists")
        intent_name = (spec.get("intent") or {}).get("name") or app.unique_name(name)
        if app.name_taken(intent_name):
            raise KxoError(f"component name '{intent_name}' already used; set intent.name to something unique")
        dlg_id = "dg-" + new_uuid()
        ic = base_component(app, intent_name, "intent")
        del ic["localeData"][app.lang]["label"]
        ic["localeData"][app.lang].update({"desc": spec.get("description", ""), "intent": name})
        ic["dialogId"] = dlg_id
        app.d["dialogComponents"].append(ic)
        dlg = app.template_dialog()
        dlg.update({
            "_id": dlg_id, "refId": new_uuid(), "lname": name.lower(), "isHidden": bool(spec.get("hidden", False)),
            "localeData": {app.lang: {"name": name, "shortDesc": spec.get("description", "")}},
            "nodes": [{
                "type": "intent", "componentId": ic["_id"], "nodeId": "intent0",
                "transitions": [{"default": "", "metadata": {"color": "#D0D5DD", "connId": "dummy0",
                                                             "pathName": "", "pathsMap": []}}],
                "metadata": {"left": 30, "top": 170}, "positionMeta": {"left": 750, "top": 110},
                "vNameSpace": [], "preConditions": [], "useTaskLevelNs": True,
                "nodeOptions": app.template_node_options("intent")}],
            "sequences": [{"name": "untitled", "nodeId": "intent0", "modified": False, "connected": False,
                           "metadata": {"left": 78.67, "top": 100}, "color": "#e7f1ff",
                           "nodes": [{"nodeId": "intent0", "type": "intent"}], "primaryIntent": True}],
        })
        app.d["dialogs"].append(dlg)
        app.d["patterns"].append({"localeData": {app.lang: {}}, "refId": ic["refId"], "taskName": intent_name,
                                  "taskType": "intent", "nodeType": "dialog"})
        sub = app.d.setdefault("exportOptions", {}).setdefault("subTasks", {}).setdefault("dialogs", [])
        if dlg_id not in sub:
            sub.append(dlg_id)
        app.reindex()
        self.set_utterances(ic, (spec.get("intent") or {}).get("utterances") or [])
        return dlg

    def update_dialog_meta(self, dlg):
        app, spec = self.app, self.spec
        loc = dlg.setdefault("localeData", {}).setdefault(app.lang, {})
        new_name = spec.get("rename")
        if new_name and new_name != loc.get("name"):
            loc["name"] = new_name
            dlg["lname"] = new_name.lower()
            self.say(f"renamed dialog -> {new_name}")
        ic = app.intent_comp(dlg)
        if "description" in spec and spec["description"] != loc.get("shortDesc", ""):
            loc["shortDesc"] = spec["description"]
            if ic:
                ic.setdefault("localeData", {}).setdefault(app.lang, {})["desc"] = spec["description"]
            self.say("description updated")
        if new_name and ic:
            ic.setdefault("localeData", {}).setdefault(app.lang, {})["intent"] = new_name
        if "hidden" in spec and bool(spec["hidden"]) != bool(dlg.get("isHidden")):
            dlg["isHidden"] = bool(spec["hidden"])
            self.say(f"hidden = {dlg['isHidden']}")
        utt = (spec.get("intent") or {}).get("utterances")
        if utt is not None and ic and sorted(utt) != sorted(app.utterances(ic)):
            self.set_utterances(ic, utt)

    def set_utterances(self, ic, utterances):
        app = self.app
        task_name = (ic.get("localeData", {}).get(app.lang) or {}).get("intent") or ic["name"]
        app.d["sentences"] = [s for s in app.d["sentences"]
                              if not (s.get("taskId") == ic["_id"] and s.get("language", app.lang) == app.lang)]
        for u in utterances:
            app.d["sentences"].append({"_id": new_object_id(), "taskId": ic["_id"], "type": "DialogIntent",
                                       "taskName": task_name, "sentence": u, "entities": [], "language": app.lang})
        self.say(f"utterances set ({len(utterances)})")

    # ---- nodes
    def resolve(self, ref):
        if ref in (None, "") or ref in SPECIAL_TARGETS:
            return ref or ""
        nid = self.label_to_id.get(str(ref).lower())
        if not nid:
            raise KxoError(f"transition target '{ref}' is not a node of dialog '{self.spec['dialog']}'")
        return nid

    def ensure_node(self, ns):
        app, dlg = self.app, self.dlg
        node = None
        if ns.get("id"):
            node = next((n for n in dlg["nodes"] if n["nodeId"] == ns["id"]), None)
            if not node:
                raise KxoError(f"node id {ns['id']} not found in dialog (drop 'id' to create a new node)")
        elif ns.get("key") and ns["key"].lower() in self.label_to_id:
            nid = self.label_to_id[ns["key"].lower()]
            node = next((n for n in dlg["nodes"] if n["nodeId"] == nid), None)
        elif not ns.get("use") and ns.get("name") and ns["name"].lower() in self.label_to_id:
            nid = self.label_to_id[ns["name"].lower()]
            node = next(n for n in dlg["nodes"] if n["nodeId"] == nid)
        if not node:
            return self.create_node(ns)
        comp = app.comp_by_id[node["componentId"]]
        if ns.get("type") and TYPE_ALIASES.get(ns["type"].lower(), ns["type"]) != comp["type"]:
            raise KxoError(f"node '{comp['name']}' is {comp['type']}, spec says {ns['type']}")
        new_name = ns.get("name") if ns.get("id") and ns.get("name") and ns["name"] != comp["name"] else None
        comp = self.edit_component(comp, ns, node=node, rename=new_name)
        if ns.get("nodeRaw"):
            deep_merge(node, ns["nodeRaw"])
        self.register(node, ns)
        return node

    def register(self, node, ns):
        """Make a node addressable by its name, its nodeId and the spec's optional 'key'."""
        comp = self.app.comp_by_id.get(node["componentId"])
        if comp:
            self.label_to_id.setdefault(comp["name"].lower(), node["nodeId"])
        self.label_to_id[node["nodeId"].lower()] = node["nodeId"]
        if ns.get("key"):
            self.label_to_id[str(ns["key"]).lower()] = node["nodeId"]

    def users_elsewhere(self, comp):
        return self.usage.get(comp["_id"], set()) - {self.me}

    def edit_component(self, comp, ns, node=None, rename=None, tool_action=False):
        """Apply spec fields (and optional rename) to an existing component. Shared components are only
        touched when the edit really changes something, and then need fork / --allow-shared."""
        app = self.app
        trial = copy.deepcopy(comp)
        changed = apply_fields(app, trial, ns, created=False) or bool(rename)
        if not changed:
            return comp
        users = self.users_elsewhere(comp)
        if users:
            if ns.get("fork") and node is not None and not tool_action:
                comp = self.fork(node, comp, ns["fork"])
            elif not self.allow_shared:
                what = (f"tool action '{comp['name']}' (part of an app-level tool, so the edit changes every "
                        "agent using it)") if tool_action else f"'{comp['name']}'"
                hint = "" if tool_action or node is None else " Add \"fork\": true to the node to copy it for this dialog, or"
                raise KxoError(f"{what} is also used by: {', '.join(sorted(users))}. Editing it changes those dialogs "
                               f"too.{hint} pass --allow-shared to edit it everywhere.")
            else:
                warn(f"'{comp['name']}' is shared with {', '.join(sorted(users))}; they change too.")
        if rename:
            self.rename_component(comp, rename)
        if apply_fields(app, comp, ns, created=False):
            self.mark_changed(comp)
            self.say(f"updated {'tool action' if tool_action else comp['type']} '{comp['name']}'")
        return comp

    def fork(self, node, comp, fork):
        fork_name = fork if isinstance(fork, str) else self.app.unique_name(comp["name"] + "_copy")
        if not NAME_RE.match(fork_name) or self.app.name_taken(fork_name):
            raise KxoError(f"fork name '{fork_name}' is invalid or already used")
        nc = copy.deepcopy(comp)
        nc.update({"_id": "dc-" + new_uuid(), "refId": new_uuid(), "name": fork_name})
        nc.setdefault("localeData", {}).setdefault(self.app.lang, {})["label"] = fork_name
        self.app.d["dialogComponents"].append(nc)
        self.app.reindex()
        node["componentId"] = nc["_id"]
        self.label_to_id[fork_name.lower()] = node["nodeId"]
        self.mark_changed(nc)
        self.say(f"forked shared '{comp['name']}' -> '{fork_name}' (other dialogs keep the original)")
        return nc

    def create_component(self, ns):
        app = self.app
        if not ns.get("name"):
            raise KxoError(f"new node needs a 'name': {json.dumps(ns)[:120]}")
        if not ns.get("type"):
            raise KxoError(f"new node '{ns['name']}' needs a 'type'")
        ctype = TYPE_ALIASES.get(ns["type"].lower())
        if not ctype:
            raise KxoError(f"unknown node type '{ns['type']}'. Known: {sorted(set(TYPE_ALIASES))}")
        if not NAME_RE.match(ns["name"]):
            raise KxoError(f"node name '{ns['name']}' must match {NAME_RE.pattern} (letters, digits, _)")
        if app.name_taken(ns["name"]):
            raise KxoError(f"component name '{ns['name']}' already exists in the app. "
                           f"Use \"use\": \"{ns['name']}\" to link it, or pick another name.")
        comp = base_component(app, ns["name"], ctype)
        init_llm_config(app, comp, ns)
        if ctype in ("message", "dialogAct", "entity") and "prompts" not in ns and "text" not in ns:
            ns = dict(ns, text=f"Sample message for {ns['name']}")
        if ctype == "entity":
            comp["entityType"] = ns.get("entityType", "string")
            if "errorPrompts" not in ns:
                ns = dict(ns, errorPrompts=[{"text": "Sorry, that doesn't look right. Please try again."}])
        if ctype == "script" and "script" not in ns:
            comp["script"] = ""
        apply_fields(app, comp, ns, created=True)
        app.d["dialogComponents"].append(comp)
        app.reindex()
        self.mark_changed(comp)
        self.say(f"created {ctype} '{comp['name']}'")
        return comp

    def create_node(self, ns):
        app = self.app
        if ns.get("use"):  # link an existing app component into this dialog
            comp = app.comp_by_lname.get(ns["use"].lower())
            if not comp:
                raise KxoError(f"use: component '{ns['use']}' not found")
            comp = self.edit_component(comp, ns)
            self.say(f"linked existing {comp['type']} '{comp['name']}'")
        else:
            comp = self.create_component(ns)
        ctype = comp["type"]
        node = {"type": ctype, "componentId": comp["_id"],
                "nodeId": f"nd-{NODE_PREFIX.get(ctype, 'nd')}-{new_uuid()}",
                "transitions": [new_transition(default="")], "vNameSpace": [], "preConditions": [],
                "useTaskLevelNs": True, "nodeOptions": app.template_node_options(ctype)}
        if ns.get("nodeRaw"):
            deep_merge(node, ns["nodeRaw"])
        self.dlg["nodes"].append(node)
        self.register(node, ns)
        return node

    def rename_component(self, comp, new):
        if not NAME_RE.match(new):
            raise KxoError(f"name '{new}' must match {NAME_RE.pattern}")
        if self.app.name_taken(new) and self.app.comp_by_lname[new.lower()] is not comp:
            raise KxoError(f"cannot rename '{comp['name']}' -> '{new}': name exists")
        self.say(f"renamed '{comp['name']}' -> '{new}' (scripts/templates that reference the old name, "
                 "e.g. context.<old name>, are NOT rewritten)")
        comp["name"] = new
        comp.setdefault("localeData", {}).setdefault(self.app.lang, {})["label"] = new
        self.app.reindex()
        nid = next((n["nodeId"] for n in self.dlg["nodes"] if n["componentId"] == comp["_id"]), None)
        if nid:
            self.label_to_id[new.lower()] = nid
        self.mark_changed(comp)

    def mark_changed(self, comp):
        self.changed_comps.add(comp["_id"])

    def apply_transitions(self, node, ns):
        if "next" in ns and "transitions" in ns:
            raise KxoError(f"node '{ns.get('name')}': use either 'next' or 'transitions', not both")
        if ("yes" in ns or "no" in ns) and ("next" in ns or "transitions" in ns):
            raise KxoError(f"node '{ns.get('name')}': use 'yes'/'no' or 'next'/'transitions', not both")
        if "yes" in ns or "no" in ns:  # patch only the given branch(es); other transitions are kept
            trans = node.setdefault("transitions", [])
            for k in ("yes", "no"):
                if k not in ns:
                    continue
                target = self.resolve(ns[k])
                t = next((x for x in trans if (x.get("if") or {}).get("dialogAct") == k), None)
                if t is None:
                    trans = [x for x in trans if "if" in x or x.get("default")]  # drop an empty placeholder
                    trans.append(new_transition(**{"if": {"dialogAct": k}, "then": target}))
                    node["transitions"] = trans
                    self.say(f"'{ns.get('name')}': added {k} -> {ns[k]}")
                elif t.get("then") != target:
                    t["then"] = target
                    self.say(f"'{ns.get('name')}': {k} -> {ns[k]}")
            return
        if "next" in ns:
            items = [{"default": ns["next"]}]
        elif "transitions" in ns:
            items = ns["transitions"]
        else:
            return
        old = node.get("transitions") or []
        new = []
        for i, it in enumerate(items):
            prev = old[i] if i < len(old) else {}
            if "default" in it:
                t = {"default": self.resolve(it["default"])}
                same_kind = "default" in prev
            elif "if" in it:
                t = {"if": it["if"], "then": self.resolve(it.get("to", it.get("then", "")))}
                same_kind = "if" in prev
            else:
                raise KxoError(f"node '{ns.get('name')}': transition needs 'default' or 'if'+'to': {it}")
            if same_kind:  # keep keys kxo does not model (e.g. isDeflectToChat)
                t = dict({k: v for k, v in prev.items() if k not in ("default", "if", "then")}, **t)
            for k in TRANSITION_EXTRAS:
                if k in it:
                    t[k] = it[k]
            t["metadata"] = prev.get("metadata") or new_transition()["metadata"]
            new.append(t)
        strip = lambda ts: [{k: v for k, v in x.items() if k != "metadata"} for x in ts]
        if strip(new) != strip(old):
            node["transitions"] = new
            self.say(f"transitions of '{ns.get('name') or node['nodeId']}' updated")

    def upsert_app_tool(self, t):
        app = self.app
        name = t["name"]
        tool = app.tool_by_name.get(name.lower())
        actions = []
        for a in t.get("actions") or []:
            c = app.comp_by_lname.get(a.lower()) if isinstance(a, str) else None
            if not c:
                raise KxoError(f"appTools '{name}': action component '{a}' must already exist (create it as a "
                               "node or tool action first)")
            actions.append({"action_id": c["refId"], "action_type": c["type"]})
        body = {"tool_name": name, "lname": name.lower(), "desc": t.get("desc", ""),
                "params": [dict({"type": "String", "isRequired": True, "params": []}, **p) for p in t.get("params", [])],
                "tool_actions": actions, "resp_path": t.get("respPath", "context." + name), "tool_source": "custom"}
        if tool:
            tool.update({k: v for k, v in body.items() if k in t or k in ("lname",)
                         or (k == "tool_actions" and "actions" in t) or (k == "resp_path" and "respPath" in t)})
            self.say(f"updated app tool '{name}'")
        else:
            body["refId"] = new_uuid()
            app.d["tools"].append(body)
            self.say(f"created app tool '{name}'")
        app.reindex()

    def apply_tools(self, node, tools_spec):
        app = self.app
        if node["type"] != "aiassist":
            raise KxoError("'tools' only valid on agent nodes")
        # Listed tools are added/updated; tools not listed stay; {"tool": X, "remove": true} detaches one.
        new = copy.deepcopy(node.get("toolInstances") or [])
        for ts in tools_spec:
            tool = app.tool_by_name.get(str(ts.get("tool", "")).lower()) or app.tool_by_ref.get(ts.get("tool"))
            if not tool:
                raise KxoError(f"tool '{ts.get('tool')}' not found in app tools (define it in 'appTools')")
            idx = next((i for i, x in enumerate(new) if x.get("toolRefId") == tool["refId"]), None)
            if ts.get("remove"):
                if idx is not None:
                    new.pop(idx)
                continue
            if idx is None:
                new.append({"nodeId": "nd-tl-" + new_uuid(), "toolRefId": tool["refId"],
                            "tool_transition": {"type": 0}, "nodes": []})
                idx = len(new) - 1
            ti = new[idx]
            if "after" in ts:
                if ts["after"] not in ("agent", "exit"):
                    raise KxoError(f"tool '{tool['tool_name']}': after must be 'agent' or 'exit'")
                ti["tool_transition"] = dict(ti.get("tool_transition") or {}, type=1 if ts["after"] == "exit" else 0)
            if "actions" in ts:
                cur = {app.comp_by_id[n["componentId"]]["name"].lower(): n for n in ti.get("nodes") or []
                       if n["componentId"] in app.comp_by_id}
                nodes = []
                for a in ts["actions"]:
                    a = {"name": a} if isinstance(a, str) else a
                    comp = app.comp_by_lname.get(a["name"].lower())
                    if comp:
                        comp = self.edit_component(comp, a, tool_action=True)
                    else:
                        comp = self.create_component(a)
                    n = cur.get(comp["name"].lower()) or {
                        "nodeId": f"nd-{NODE_PREFIX.get(comp['type'], 'nd')}-{new_uuid()}",
                        "componentId": comp["_id"], "type": comp["type"],
                        "nodeOptions": app.template_node_options(comp["type"])}
                    nodes.append(n)
                    acts = tool.setdefault("tool_actions", [])
                    if not any(x.get("action_id") == comp["refId"] for x in acts):
                        acts.append({"action_id": comp["refId"], "action_type": comp["type"]})
                        self.say(f"app tool '{tool['tool_name']}' now runs '{comp['name']}' (affects every agent "
                                 "using this tool)")
                ti["nodes"] = nodes
        strip = lambda xs: json.dumps(xs, sort_keys=True)
        if strip(new) != strip(node.get("toolInstances") or []):
            node["toolInstances"] = new
            self.say(f"tools of '{app.comp_by_id[node['componentId']]['name']}' updated")

    def remove_node(self, ref):
        nid = self.resolve(ref)
        dlg = self.dlg
        if nid == "intent0":
            raise KxoError("cannot remove the intent node")
        dlg["nodes"] = [n for n in dlg["nodes"] if n["nodeId"] != nid]
        dangling = []
        for n in dlg["nodes"]:
            for t in n.get("transitions") or []:
                for k in ("default", "then"):
                    if t.get(k) == nid:
                        t[k] = ""
                        dangling.append(n["nodeId"])
        self.say(f"removed node '{ref}'" + (f"; {len(dangling)} transition(s) now unconnected" if dangling else ""))

    def sync_sequences(self):
        """Keep the canvas layout (sequences) in step with nodes: drop removed, add new on a grid."""
        dlg = self.dlg
        ids = {n["nodeId"]: n for n in dlg["nodes"]}
        seqs = []
        placed = set()
        for s in dlg.get("sequences") or []:
            s["nodes"] = [x for x in s.get("nodes", []) if x.get("nodeId") in ids]
            if s["nodes"] or s.get("primaryIntent"):
                seqs.append(s)
                placed.update(x["nodeId"] for x in s["nodes"])
        missing = [n for n in dlg["nodes"] if n["nodeId"] not in placed]
        if missing:
            lefts = [(s.get("metadata") or {}).get("left", 0) for s in seqs] or [0]
            x0 = max(lefts) + 420
            num = len(seqs)
            for i, n in enumerate(missing):
                num += 1
                seqs.append({"name": f"Sequence {num}", "nodeId": "nd-seq-" + new_uuid(), "modified": False,
                             "connected": False,
                             "metadata": {"left": x0 + (i % 4) * 420, "top": 100 + (i // 4) * 380},
                             "colorMeta": {"paletColor": "#6693CC", "bgColor": "#D9E4F2", "borderColor": "#6693CC",
                                           "seqTitleColor": "#101828", "isSystemColor": True},
                             "nodes": [{"nodeId": n["nodeId"], "type": n["type"]}]})
        dlg["sequences"] = seqs


# --------------------------------------------------------------------------- validation
def validate(app):
    errs, warns = [], []
    names = {}
    for c in app.d["dialogComponents"]:
        names.setdefault(c.get("name", "").lower(), []).append(c["_id"])
    for n, ids in names.items():
        if len(ids) > 1:
            errs.append(f"duplicate component name '{n}' ({len(ids)} components)")
    refs = {}
    for x in app.d["dialogs"] + app.d["dialogComponents"]:
        if x.get("refId"):
            refs.setdefault(x["refId"], []).append(x["_id"])
    for r, ids in refs.items():
        if len(ids) > 1:
            errs.append(f"duplicate refId {r}: {ids}")
    sub = set(((app.d.get("exportOptions") or {}).get("subTasks") or {}).get("dialogs") or [])
    tool_refs = set(app.tool_by_ref)
    comp_refs = set(app.comp_by_ref)
    for dlg in app.d["dialogs"]:
        dn = app.dname(dlg)
        ids = [n["nodeId"] for n in iter_all_nodes(dlg)]
        if len(ids) != len(set(ids)):
            errs.append(f"[{dn}] duplicate nodeIds")
        top = {n["nodeId"] for n in dlg["nodes"]}
        if "intent0" not in top:
            errs.append(f"[{dn}] has no intent0 node")
        ic = app.intent_comp(dlg)
        if ic and ic.get("dialogId") and ic["dialogId"] != dlg["_id"]:
            errs.append(f"[{dn}] intent component dialogId mismatch")
        if sub and dlg["_id"] not in sub:
            warns.append(f"[{dn}] not listed in exportOptions.subTasks.dialogs")
        for n in iter_all_nodes(dlg):
            if n["componentId"] not in app.comp_by_id:
                errs.append(f"[{dn}] node {n['nodeId']} -> missing component {n['componentId']}")
        for n in dlg["nodes"]:
            for t in n.get("transitions") or []:
                tgt = t.get("default", t.get("then"))
                if tgt not in ("", None) and tgt not in SPECIAL_TARGETS and tgt not in top:
                    errs.append(f"[{dn}] {n['nodeId']} -> unknown node {tgt}")
                if tgt == "" and n["nodeId"] != "intent0":
                    warns.append(f"[{dn}] {node_name(app, n)} has an unconnected transition")
            for ti in n.get("toolInstances") or []:
                if ti.get("toolRefId") not in tool_refs:
                    errs.append(f"[{dn}] {node_name(app, n)} uses unknown tool {ti.get('toolRefId')}")
        # reachability
        graph = {n["nodeId"]: [t.get("default", t.get("then")) for t in n.get("transitions") or []]
                 for n in dlg["nodes"]}
        seen, stack = set(), ["intent0"]
        while stack:
            x = stack.pop()
            if x in seen or x not in graph:
                continue
            seen.add(x)
            stack.extend(graph[x])
        for n in dlg["nodes"]:
            if n["nodeId"] not in seen:
                warns.append(f"[{dn}] {node_name(app, n)} is unreachable from the intent")
        seq_nodes = {x["nodeId"] for s in dlg.get("sequences") or [] for x in s.get("nodes", [])}
        for n in dlg["nodes"]:
            if n["nodeId"] not in seq_nodes:
                warns.append(f"[{dn}] {node_name(app, n)} is not on the canvas (no sequence)")
    for t in app.d["tools"]:
        for a in t.get("tool_actions") or []:
            if a.get("action_id") not in comp_refs:
                errs.append(f"tool '{t.get('tool_name')}' action {a.get('action_id')} -> missing component")
    for s in app.d["sentences"]:
        if s.get("taskId") not in app.comp_by_id and s.get("type") == "DialogIntent":
            warns.append(f"utterance '{s.get('sentence')}' points to missing intent {s.get('taskId')}")
    for c in app.d["dialogComponents"]:
        if not NAME_RE.match(c.get("name", "")):
            warns.append(f"component name '{c.get('name')}' has unusual characters")
    return errs, warns


def node_name(app, n):
    c = app.comp_by_id.get(n["componentId"])
    return c["name"] if c else n["nodeId"]


# --------------------------------------------------------------------------- diff
def diff(app):
    orig_path = os.path.join(app.workdir, STATE_DIR, ORIGINAL)
    if not os.path.isfile(orig_path):
        raise KxoError("no original snapshot; was this folder created with `kxo init`?")
    o = load_json(orig_path)
    oc = {c["refId"]: c for c in o.get("dialogComponents", [])}
    od = {d["refId"]: d for d in o.get("dialogs", [])}
    changed_comp = {c["_id"] for c in app.d["dialogComponents"]
                    if c["refId"] not in oc or json.dumps(oc[c["refId"]], sort_keys=True) != json.dumps(c, sort_keys=True)}
    o_sent = {}
    for s in o.get("sentences", []):
        o_sent.setdefault(s.get("taskId"), set()).add((s.get("sentence"), s.get("language")))
    n_sent = {}
    for s in app.d["sentences"]:
        n_sent.setdefault(s.get("taskId"), set()).add((s.get("sentence"), s.get("language")))
    res = {"new": [], "changed": [], "removed": [], "tools": []}
    for dlg in app.d["dialogs"]:
        nm = app.dname(dlg)
        if dlg["refId"] not in od:
            res["new"].append((nm, dlg["_id"]))
            continue
        why = []
        if json.dumps(od[dlg["refId"]], sort_keys=True) != json.dumps(dlg, sort_keys=True):
            why.append("flow/settings")
        comps = [app.comp_by_id[n["componentId"]]["name"] for n in iter_all_nodes(dlg)
                 if n["componentId"] in changed_comp and n["componentId"] in app.comp_by_id]
        if comps:
            why.append("nodes: " + ", ".join(sorted(set(comps))))
        ic = app.intent_comp(dlg)
        if ic and o_sent.get(ic["_id"], set()) != n_sent.get(ic["_id"], set()):
            why.append("utterances")
        if why:
            res["changed"].append((nm, dlg["_id"], "; ".join(why)))
    now = {d["refId"] for d in app.d["dialogs"]}
    for r, d in od.items():
        if r not in now:
            res["removed"].append(d.get("localeData", {}).get(app.lang, {}).get("name", d["_id"]))
    otools = {t["refId"]: t for t in o.get("tools", [])}
    for t in app.d["tools"]:
        if t["refId"] not in otools:
            res["tools"].append(f"new tool {t['tool_name']}")
        elif json.dumps(otools[t["refId"]], sort_keys=True) != json.dumps(t, sort_keys=True):
            res["tools"].append(f"changed tool {t['tool_name']}")
    return res


# --------------------------------------------------------------------------- printing
def show(app, dlg):
    spec = dialog_to_spec(app, dlg)
    print(f"Dialog: {spec['dialog']}   refId={spec['refId']}   _id={dlg['_id']}   hidden={spec['hidden']}")
    if spec["description"]:
        print(f"  description: {spec['description']}")
    it = spec["intent"]
    print(f"  intent: {it['name']}   utterances: {it['utterances']}")
    print(f"  start -> {spec.get('start') or '(unconnected)'}")
    for n in spec["nodes"]:
        sh = f"  [shared with: {', '.join(n['shared'])}]" if n.get("shared") else ""
        print(f"\n  * {n.get('name')} ({n['type']}){sh}")
        for k in ("entityType", "method", "url", "model", "intentPath"):
            if n.get(k):
                print(f"      {k}: {n[k]}")
        for p in n.get("prompts", []):
            print(f"      prompt[{p.get('type', 'basic')}]: {short(p['text'])}")
        for k in ("script", "prompt", "systemContext", "body"):
            if n.get(k):
                print(f"      {k}: {short(n[k])}")
        for t in n.get("tools", []):
            print(f"      tool {t['tool']} (after: {t['after']}) actions: {[a['name'] for a in t['actions']]}")
        if "next" in n:
            print(f"      -> {n['next'] or '(unconnected)'}")
        for t in n.get("transitions", []):
            if "default" in t:
                print(f"      else -> {t['default'] or '(unconnected)'}")
            else:
                print(f"      if {json.dumps(t['if'])} -> {t['to'] or '(unconnected)'}")


def short(s, n=110):
    s = str(s).replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "..."


# --------------------------------------------------------------------------- Kore public API (optional)
def jwt_token(client_id, client_secret):
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": str(int(time.time())), "appId": client_id}).encode())
    sig = b64(hmac.new(client_secret.encode(), head + b"." + body, hashlib.sha256).digest())
    return (head + b"." + body + b"." + sig).decode()


def api(host, path, token, data=None, method=None, files=None):
    url = host.rstrip("/") + path
    headers = {"auth": token}
    body = None
    if files:
        boundary = "kxo" + uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in (data or {}).items():
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        for k, (fname, content) in files.items():
            buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n".encode())
            buf.write(content)
            buf.write(b"\r\n")
        buf.write(f"--{boundary}--\r\n".encode())
        body = buf.getvalue()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise KxoError(f"{e.code} {path}: {e.read().decode(errors='replace')[:500]}")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def api_creds(a):
    host = a.host or os.environ.get("KORE_HOST")
    bot = a.bot or os.environ.get("KORE_BOT_ID")
    cid = os.environ.get("KORE_CLIENT_ID")
    sec = os.environ.get("KORE_CLIENT_SECRET")
    missing = [n for n, v in (("--host/KORE_HOST", host), ("--bot/KORE_BOT_ID", bot),
                              ("KORE_CLIENT_ID", cid), ("KORE_CLIENT_SECRET", sec)) if not v]
    if missing:
        raise KxoError("missing: " + ", ".join(missing) + ". The client id/secret come from an app created in "
                       "Admin Console > API Scopes with 'Bot Export'/'Bot Import' scope.")
    return host, bot, jwt_token(cid, sec)


# --------------------------------------------------------------------------- commands
def cmd_init(a):
    src = a.export
    dst = os.path.abspath(a.workdir)
    if os.path.exists(os.path.join(dst, APPDEF)) and not a.force:
        raise KxoError(f"{dst} already has {APPDEF}; use --force to overwrite")
    os.makedirs(os.path.join(dst, STATE_DIR), exist_ok=True)
    if os.path.isdir(src):
        for f in os.listdir(src):
            p = os.path.join(src, f)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(dst, f))
    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            for m in z.namelist():
                base = os.path.basename(m)
                if base and not m.endswith("/"):
                    with open(os.path.join(dst, base), "wb") as f:
                        f.write(z.read(m))
    else:
        raise KxoError(f"{src} is neither a folder nor a zip")
    if not os.path.isfile(os.path.join(dst, APPDEF)):
        raise KxoError(f"no {APPDEF} found in {src}")
    shutil.copy2(os.path.join(dst, APPDEF), os.path.join(dst, STATE_DIR, ORIGINAL))
    app = App(dst)
    info(f"Workspace ready: {dst}  (app '{(load_json(os.path.join(dst, CONFIG)) if os.path.isfile(os.path.join(dst, CONFIG)) else {}).get('name', '?')}', "
         f"{len(app.d['dialogs'])} dialogs, {len(app.d['dialogComponents'])} nodes/components, language {app.lang})")


def cmd_list(a):
    app = App(a.workdir)
    rows = []
    for d in app.d["dialogs"]:
        if d.get("isHidden") and not a.all:
            continue
        ic = app.intent_comp(d)
        rows.append((app.dname(d), len(d["nodes"]) - 1, len(app.utterances(ic)), "hidden" if d.get("isHidden") else "",
                     d["refId"]))
    w = max([len(r[0]) for r in rows] + [6])
    print(f"{'DIALOG'.ljust(w)}  NODES  UTTS  {'':6}  REFID")
    for r in sorted(rows):
        print(f"{r[0].ljust(w)}  {r[1]:>5}  {r[2]:>4}  {r[3]:6}  {r[4]}")
    if not a.all:
        print("(hidden/system dialogs omitted; --all to show)")


def cmd_show(a):
    app = App(a.workdir)
    show(app, app.find_dialog(a.dialog))


def cmd_get(a):
    app = App(a.workdir)
    spec = dialog_to_spec(app, app.find_dialog(a.dialog), raw=a.raw)
    out = json.dumps(spec, indent=2, ensure_ascii=False)
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(out + "\n")
        info(f"wrote {a.out}")
    else:
        print(out)


def cmd_apply(a):
    app = App(a.workdir)
    specs = load_json(a.spec)
    specs = specs if isinstance(specs, list) else [specs]
    before = json.dumps(app.d, sort_keys=True)
    touched = set()
    for spec in specs:
        ap = Applier(app, spec, allow_shared=a.allow_shared)
        dlg, created = ap.run()
        app.reindex()
        touched.add(app.dname(dlg))
        info(f"[{app.dname(dlg)}]")
        for m in ap.log or ["no changes"]:
            info(f"  {m}")
    errs, warns = validate(app)
    for w in warns:
        if any(w.startswith(f"[{t}]") for t in touched):
            warn(w)
    if errs:
        for e in errs:
            print("ERROR: " + e, file=sys.stderr)
        raise KxoError("validation failed; nothing saved")
    if json.dumps(app.d, sort_keys=True) == before:
        info("nothing to save")
    elif a.dry_run:
        info("dry run: nothing saved")
    else:
        app.save()
        info(f"saved {app.path}")


def cmd_clone(a):
    app = App(a.workdir)
    src = app.find_dialog(a.dialog)
    if app.find_dialog(a.new_name, required=False):
        raise KxoError(f"dialog '{a.new_name}' already exists")
    spec = dialog_to_spec(app, src)
    label_to_old = {v.lower(): k for k, v in node_label_map(app, src).items()}

    def old_key(r):  # transitions refer to source nodes by their (unique) old nodeId, used as spec 'key'
        if r in (None, "") or r in SPECIAL_TARGETS:
            return r
        return label_to_old.get(str(r).lower(), r)

    suffix = a.suffix or "_" + re.sub(r"[^A-Za-z0-9]", "", a.new_name)[:20]
    rename, nodes = {}, []
    for n in spec["nodes"]:
        oid, old, ntype = n.get("id"), n["name"], n["type"]
        flow = {k: n[k] for k in ("next", "transitions") if k in n}
        if ntype == "intent":  # a link to another dialog's intent: reuse it, don't copy
            e = {"name": old, "use": old}
        elif old.lower() in rename:  # same component used by several nodes of the source dialog
            e = {"name": rename[old.lower()], "use": rename[old.lower()]}
        else:
            rename[old.lower()] = app.unique_name(old + suffix)
            e = {k: v for k, v in n.items() if k not in ("id", "shared", "model", "tools", "next", "transitions")}
            e.update(name=rename[old.lower()], llmTemplate=old)
        e["key"] = oid
        if "next" in flow:
            e["next"] = old_key(flow["next"])
        if "transitions" in flow:
            e["transitions"] = [dict(t, **({"default": old_key(t["default"])} if "default" in t
                                           else {"to": old_key(t["to"])})) for t in flow["transitions"]]
        nodes.append(e)
    spec.update({"dialog": a.new_name, "refId": None, "nodes": nodes, "start": old_key(spec.get("start", ""))})
    spec["intent"] = {"name": app.unique_name(a.new_name), "utterances": a.utterance or []}
    if a.description is not None:
        spec["description"] = a.description
    ap = Applier(app, spec)
    dlg, _ = ap.run()
    # carry over everything kxo does not model (IVR, rephrase, sample responses, node options, tool instances)
    copied = set()
    for n in src["nodes"]:
        if n["nodeId"] == "intent0":
            continue
        nn = next(x for x in dlg["nodes"] if x["nodeId"] == ap.label_to_id[n["nodeId"].lower()])
        nn["nodeOptions"] = copy.deepcopy(n.get("nodeOptions", nn.get("nodeOptions")))
        if n.get("toolInstances"):
            tis = copy.deepcopy(n["toolInstances"])
            for ti in tis:
                ti["nodeId"] = "nd-tl-" + new_uuid()
                for tn in ti.get("nodes") or []:
                    tn["nodeId"] = f"nd-{NODE_PREFIX.get(tn.get('type'), 'nd')}-{new_uuid()}"
            nn["toolInstances"] = tis
        sc = app.comp_by_id.get(n["componentId"])
        nc = app.comp_by_id.get(nn["componentId"])
        if sc and nc and sc is not nc and nc["_id"] not in copied:
            copied.add(nc["_id"])
            keep = {k: nc[k] for k in ("_id", "refId", "name")}
            nc.clear()
            nc.update(copy.deepcopy(sc))
            nc.update(keep)
            nc.setdefault("localeData", {}).setdefault(app.lang, {})["label"] = keep["name"]
    errs, warns = validate(app)
    for w in warns:
        if w.startswith(f"[{a.new_name}]"):
            warn(w)
    if errs:
        for e in errs:
            print("ERROR: " + e, file=sys.stderr)
        raise KxoError("validation failed; nothing saved")
    app.save()
    info(f"cloned '{app.dname(src)}' -> '{a.new_name}' with {len(copied)} copied components "
         f"(agent tools and their actions are shared, not copied). Scripts that reference old node names "
         f"(e.g. context.<NodeName>) still use the OLD names - review them.")


def cmd_delete_dialog(a):
    app = App(a.workdir)
    dlg = app.find_dialog(a.dialog)
    ic = app.intent_comp(dlg)
    mine = {n["componentId"] for n in iter_all_nodes(dlg)}
    app.d["dialogs"].remove(dlg)
    used = set()
    for d in app.d["dialogs"]:
        used.update(n["componentId"] for n in iter_all_nodes(d))
    tool_actions = {x.get("action_id") for t in app.d["tools"] for x in t.get("tool_actions") or []}
    gone = [c for c in app.d["dialogComponents"]
            if c["_id"] in mine and c["_id"] not in used and c.get("refId") not in tool_actions]
    gone_ids = {c["_id"] for c in gone}
    app.d["dialogComponents"] = [c for c in app.d["dialogComponents"] if c["_id"] not in gone_ids]
    if ic:
        app.d["sentences"] = [s for s in app.d["sentences"] if s.get("taskId") != ic["_id"]]
    app.d["patterns"] = [p for p in app.d["patterns"] if p.get("refId") not in {c.get("refId") for c in gone}]
    sub = ((app.d.get("exportOptions") or {}).get("subTasks") or {}).get("dialogs")
    if sub and dlg["_id"] in sub:
        sub.remove(dlg["_id"])
    app.save()
    info(f"removed '{app.dname(dlg)}' and {len(gone)} components only it used from the export file. NOTE: an "
         "Incremental Import does NOT delete dialogs in Kore - delete it in the UI too (or use a Full Import).")


def cmd_validate(a):
    app = App(a.workdir)
    errs, warns = validate(app)
    for w in warns:
        print("warn : " + w)
    for e in errs:
        print("ERROR: " + e)
    print(f"{len(errs)} error(s), {len(warns)} warning(s)")
    if errs:
        sys.exit(1)


def cmd_diff(a):
    app = App(a.workdir)
    r = diff(app)
    for nm, i in r["new"]:
        print(f"NEW      {nm}  ({i})")
    for nm, i, why in r["changed"]:
        print(f"CHANGED  {nm}  ({i})  - {why}")
    for nm in r["removed"]:
        print(f"REMOVED  {nm}  (incremental import will not delete it in Kore)")
    for t in r["tools"]:
        print(f"TOOLS    {t}")
    if not any(r.values()):
        print("no changes")


def build_files(app, out, strip_hash=False):
    errs, _ = validate(app)
    if errs:
        for e in errs:
            print("ERROR: " + e, file=sys.stderr)
        raise KxoError("fix validation errors first (kxo validate)")
    out = os.path.abspath(out or os.path.join(app.workdir, "build"))
    os.makedirs(out, exist_ok=True)
    data = copy.deepcopy(app.d)
    if strip_hash:
        data.pop("latestEncryptedHash", None)
        data.pop("encryptedHash", None)
    save_json(os.path.join(out, APPDEF), data)
    for f in (CONFIG, "icon.png", "botFunctions.js"):
        p = os.path.join(app.workdir, f)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(out, f))
    name = "app"
    if os.path.isfile(os.path.join(app.workdir, CONFIG)):
        name = re.sub(r"[^A-Za-z0-9 _()-]", "", load_json(os.path.join(app.workdir, CONFIG)).get("name", "app"))
    zpath = os.path.join(out, f"{name} - kxo.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in (APPDEF, CONFIG, "icon.png", "botFunctions.js"):
            p = os.path.join(out, f)
            if os.path.isfile(p):
                z.write(p, f)
    return out, zpath


def cmd_build(a):
    app = App(a.workdir)
    out, zpath = build_files(app, a.out, a.strip_hash)
    r = diff(app)
    info(f"Built {out}\n  {APPDEF}, {CONFIG}{', icon.png' if os.path.isfile(os.path.join(out, 'icon.png')) else ''}"
         f"\n  {zpath}")
    print("\nImport in Kore: App Settings > App Management > Import (Incremental Import), upload the files above,")
    print("select Dialog tasks + NLP data (utterances), and pick these dialogs:")
    for nm, _ in r["new"]:
        print(f"  [new]     {nm}")
    for nm, _, why in r["changed"]:
        print(f"  [changed] {nm}  ({why})")
    if r["tools"]:
        print("  app tools changed: " + "; ".join(r["tools"]))
    if not (r["new"] or r["changed"] or r["tools"]):
        print("  (nothing changed vs the original export)")


def cmd_export_api(a):
    host, bot, tok = api_creds(a)
    r = api(host, f"/api/public/bot/{bot}/export", tok, {"exportType": a.type})
    info(f"export requested: {r}")
    for _ in range(60):
        time.sleep(3)
        st = api(host, f"/api/public/bot/{bot}/export/status", tok)
        if isinstance(st, dict) and st.get("status") in ("success", "SUCCESS") and st.get("downloadURL"):
            dest = os.path.abspath(a.out)
            urllib.request.urlretrieve(st["downloadURL"], dest)
            info(f"downloaded {dest}  ->  next: kxo init \"{dest}\" <workdir>")
            return
        if isinstance(st, dict) and st.get("status") in ("failed", "FAILED"):
            raise KxoError(f"export failed: {st}")
    raise KxoError("export did not finish in 3 minutes")


def cmd_deploy(a):
    app = App(a.workdir)
    host, bot, tok = api_creds(a)
    out, _ = build_files(app, a.build, a.strip_hash)  # always rebuild so uploaded file == current workspace
    info(f"rebuilt {out}")
    ids = {}
    for f in (APPDEF, CONFIG):
        if a.dry_run:
            ids[f] = f"<fileId of uploaded {f}>"
            continue
        with open(os.path.join(out, f), "rb") as fh:
            r = api(host, "/api/public/uploadfile", tok, {"fileContext": "bulkImport", "fileExtension": "json"},
                    files={"file": (f, fh.read())})
        ids[f] = r["fileId"]
        info(f"uploaded {f}: {r['fileId']}")
    r = diff(app)
    dialog_ids = [i for _, i in r["new"]] + [i for _, i, _ in r["changed"]]
    if r["tools"]:
        warn("app-level tools changed (" + "; ".join(r["tools"]) + "); the dialog import may not carry them - "
             "check the tools in Kore after import")
    body = {"botDefinition": ids[APPDEF], "configInfo": ids[CONFIG]}
    if a.full:
        body["importOptions"] = {}
    else:
        if not dialog_ids:
            raise KxoError("no changed dialogs to import")
        body["importOptions"] = {"nlpData": ["utterances"], "tasks": ["botTask"],
                                 "subTasks": {"dialogs": dialog_ids},
                                 "options": {"utterances": {"append": False, "replace": True}}}
    if a.dry_run:
        print(json.dumps(body, indent=2))
        return
    res = api(host, f"/api/public/bot/{bot}/import", tok, body)
    bir = res.get("_id") if isinstance(res, dict) else None
    info(f"import requested: {res}")
    if not bir:
        return
    for _ in range(100):
        time.sleep(3)
        st = api(host, f"/api/public/bot/import/status/{bir}", tok)
        s = st.get("status") if isinstance(st, dict) else st
        if s not in ("pending", "inprogress", "in-progress", "IN_PROGRESS"):
            info(f"import status: {json.dumps(st)[:1500]}")
            return
    raise KxoError("import still pending after 5 minutes; check the app in Kore")


def main(argv=None):
    p = argparse.ArgumentParser(prog="kxo", description="Edit Kore.ai XO dialog tasks in an app export.")
    sp = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        s = sp.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    s = add("init", cmd_init, "create a workspace from an export folder or zip")
    s.add_argument("export")
    s.add_argument("workdir")
    s.add_argument("--force", action="store_true")
    for name, fn, h in (("list", cmd_list, "list dialogs"), ("show", cmd_show, "print a dialog flow"),
                        ("get", cmd_get, "dialog -> editable JSON spec"), ("validate", cmd_validate, "check integrity"),
                        ("diff", cmd_diff, "what changed vs the original export"),
                        ("build", cmd_build, "write import-ready files"),
                        ("apply", cmd_apply, "create/update dialogs from a spec"),
                        ("clone", cmd_clone, "copy a dialog as a new dialog"),
                        ("delete-dialog", cmd_delete_dialog, "remove a dialog from the export"),
                        ("deploy", cmd_deploy, "BETA: import via Kore public API (needs JWT app)")):
        s = add(name, fn, h)
        s.add_argument("-w", "--workdir", default=os.environ.get("KXO_WORKDIR", "."))
        if name in ("show", "get", "clone", "delete-dialog"):
            s.add_argument("dialog")
        if name == "get":
            s.add_argument("-o", "--out")
            s.add_argument("--raw", action="store_true", help="include the full component JSON under 'raw'")
        if name == "apply":
            s.add_argument("spec")
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--allow-shared", action="store_true", help="allow editing nodes shared with other dialogs")
        if name == "clone":
            s.add_argument("new_name")
            s.add_argument("--utterance", action="append")
            s.add_argument("--description")
            s.add_argument("--suffix", help="suffix for copied node names (default: _<NewName>)")
        if name == "list":
            s.add_argument("--all", action="store_true")
        if name == "build":
            s.add_argument("-o", "--out")
            s.add_argument("--strip-hash", action="store_true",
                           help="drop encryptedHash fields (try only if Kore rejects the edited file)")
        if name == "deploy":
            s.add_argument("--host")
            s.add_argument("--bot")
            s.add_argument("--build", help="build output folder (default <workdir>/build); rebuilt before upload")
            s.add_argument("--strip-hash", action="store_true")
            s.add_argument("--full", action="store_true")
            s.add_argument("--dry-run", action="store_true")
    s = add("export-api", cmd_export_api, "BETA: download a fresh export via Kore public API (needs JWT app)")
    s.add_argument("--host")
    s.add_argument("--bot")
    s.add_argument("--type", default="latest", choices=["latest", "published"])
    s.add_argument("-o", "--out", default="export.zip")
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except KxoError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
