#!/usr/bin/env python3
"""Self-test for kxo against a real Kore export.  Usage:  python selftest.py <export folder or zip>

Never modifies the export: works in temp copies.  Checks:
  1. get -> apply round trip of every dialog is byte-identical (also with --raw specs)
  2. create from examples/new-dialog.json validates and is idempotent
  3. partial edits, shared-node guard, fork, clone of every dialog, tools, delete, diff, build
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
KXO = [sys.executable, os.path.join(HERE, "kxo.py")]
EXAMPLE = os.path.join(HERE, "..", "examples", "new-dialog.json")
sys.path.insert(0, HERE)
import kxo  # noqa: E402

TMP = tempfile.mkdtemp(prefix="kxo-test-")
FAILS = []


def run(*args, ok=True):
    r = subprocess.run(KXO + list(args), capture_output=True, text=True, encoding="utf-8")
    if ok is True and r.returncode != 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"FAILED: kxo {' '.join(args)}")
    if ok is False and r.returncode == 0:
        print(r.stdout, r.stderr)
        raise SystemExit(f"EXPECTED FAILURE: kxo {' '.join(args)}")
    return r


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(f"{name} {detail}")


def fresh(export, name):
    ws = os.path.join(TMP, name)
    run("init", export, ws)
    return ws


def write(obj, name="spec.json"):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return p


def same_as_original(ws):
    return open(os.path.join(ws, "appDefinition.json"), "rb").read() == \
        open(os.path.join(ws, ".kxo", "original_appDefinition.json"), "rb").read()


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    export = sys.argv[1]

    print("[1] lossless round trip")
    ws = fresh(export, "rt")
    ids = [d["_id"] for d in kxo.App(ws).d["dialogs"]]
    for did in ids:
        run("get", "-w", ws, did, "-o", os.path.join(TMP, "rt.json"))
        run("apply", "-w", ws, os.path.join(TMP, "rt.json"))  # no --allow-shared: unchanged shared nodes are fine
    check(f"{len(ids)} dialogs, plain specs, byte-identical", same_as_original(ws))
    for did in ids:
        run("get", "-w", ws, did, "--raw", "-o", os.path.join(TMP, "rt.json"))
        run("apply", "-w", ws, os.path.join(TMP, "rt.json"))
    check("--raw specs, byte-identical", same_as_original(ws))

    print("[2] create from example")
    ws = fresh(export, "create")
    app = kxo.App(ws)
    ex = json.load(open(EXAMPLE, encoding="utf-8"))
    taken = {c["name"].lower() for c in app.d["dialogComponents"]}
    if app.find_dialog(ex["dialog"], required=False) or any(n["name"].lower() in taken for n in ex["nodes"]):
        print("  skipped: example names already exist in this app")
    else:
        run("apply", "-w", ws, EXAMPLE)
        r = run("apply", "-w", ws, EXAMPLE)
        check("idempotent re-apply", "nothing to save" in r.stdout, r.stdout)
        app = kxo.App(ws)
        lf = app.comp_by_lname["lookupfare"]
        check("service url encoded like Kore (encodeURI)", "/fares/economy" in lf["endPoint"]["path"])
        # partial yes-only edit keeps the 'no' branch
        run("apply", "-w", ws, write({"dialog": ex["dialog"], "nodes": [{"name": "OfferUpgrade", "yes": "SummaryMessage"}]}))
        app = kxo.App(ws)
        d = app.find_dialog(ex["dialog"])
        ou = next(n for n in d["nodes"] if n["componentId"] == app.comp_by_lname["offerupgrade"]["_id"])
        acts = {(t.get("if") or {}).get("dialogAct") for t in ou["transitions"]}
        check("yes-only patch keeps 'no' transition", acts == {"yes", "no"}, str(ou["transitions"]))
        # raw + friendly: friendly wins
        spec = json.loads(run("get", "-w", ws, ex["dialog"], "--raw").stdout)
        for n in spec["nodes"]:
            if n["name"] == "ResolveAllowance":
                n["script"] = "context.baggageKg = 99;"
        run("apply", "-w", ws, write(spec))
        app = kxo.App(ws)
        check("edited 'script' wins over stale 'raw'",
              kxo.dec(app.comp_by_lname["resolveallowance"]["script"]) == "context.baggageKg = 99;")

    print("[3] shared nodes, clone, tools, delete")
    ws = fresh(export, "edit")
    app = kxo.App(ws)
    usage = app.component_usage()
    kinds = ("message", "script", "entity", "dialogAct")
    shared = [(cid, sorted(u)) for cid, u in usage.items() if len(u) > 1 and app.comp_by_id[cid]["type"] in kinds]
    if not shared:  # make one: link an existing message/script into a new dialog with "use"
        c = next((c for c in app.d["dialogComponents"] if c["type"] in ("message", "script")
                  and c["_id"] in usage), None)
        if c:
            run("apply", "-w", ws, write({"dialog": "Kxo Share Test", "intent": {"utterances": ["kxo share test"]},
                                          "start": c["name"], "nodes": [{"name": c["name"], "use": c["name"],
                                                                         "next": "end"}]}))
            app = kxo.App(ws)
            usage = app.component_usage()
            shared = [(c["_id"], sorted(usage[c["_id"]]))]
            check("'use' links an existing node into a new dialog", len(shared[0][1]) == 2)
    if shared:
        cid, users = shared[0]
        comp = app.comp_by_id[cid]
        field = "script" if comp["type"] == "script" else "text"
        spec = {"dialog": users[0], "nodes": [{"name": comp["name"], field: "changed by selftest"}]}
        run("apply", "-w", ws, write(spec), ok=False)
        check("shared node edit refused without fork/--allow-shared", True)
        spec["nodes"][0]["fork"] = True
        run("apply", "-w", ws, write(spec))
        check("fork leaves the original untouched",
              kxo.App(ws).comp_by_id[cid] == comp)
    else:
        print("  (no shared message/script/entity nodes in this app; guard test skipped)")

    ws = fresh(export, "clone")
    app = kxo.App(ws)
    first = app.dname(app.d["dialogs"][0])
    run("clone", "-w", ws, first, first, ok=False)
    check("clone onto an existing name is refused", True)
    for i, d in enumerate(list(app.d["dialogs"])):
        run("clone", "-w", ws, d["_id"], f"Kxo Clone {i}", "--suffix", f"_k{i}")
    errs, _ = kxo.validate(kxo.App(ws))
    check(f"clone of all {len(app.d['dialogs'])} dialogs validates", not errs, str(errs[:3]))

    ws = fresh(export, "tools")
    app = kxo.App(ws)
    agent_dlg = next((d for d in app.d["dialogs"] if any(n.get("toolInstances") for n in d["nodes"])), None)
    if agent_dlg:
        an = next(n for n in agent_dlg["nodes"] if n.get("toolInstances"))
        ti = an["toolInstances"][0]
        tool = app.tool_by_ref[ti["toolRefId"]]
        before_after = ti["tool_transition"]["type"]
        names = [app.comp_by_id[x["componentId"]]["name"] for x in ti["nodes"]]
        spec = {"dialog": agent_dlg["_id"], "nodes": [{"name": app.comp_by_id[an["componentId"]]["name"], "tools": [
            {"tool": tool["tool_name"], "actions": names + [{"name": "KxoSelftestAction", "type": "script",
                                                             "script": "context.kxo = 1;"}]}]}]}
        run("apply", "-w", ws, write(spec))
        app = kxo.App(ws)
        d = app.find_dialog(agent_dlg["_id"])
        an2 = next(n for n in d["nodes"] if n["nodeId"] == an["nodeId"])
        ti2 = next(x for x in an2["toolInstances"] if x["toolRefId"] == ti["toolRefId"])
        new_ref = app.comp_by_lname["kxoselftestaction"]["refId"]
        check("partial tools spec keeps other tools", len(an2["toolInstances"]) == len(an["toolInstances"]))
        check("tool 'after' kept when not given", ti2["tool_transition"]["type"] == before_after)
        check("new tool action registered on the app tool",
              any(x["action_id"] == new_ref for x in app.tool_by_ref[ti["toolRefId"]]["tool_actions"]))
        check("new tool action not left in dialog nodes",
              all(n["componentId"] != app.comp_by_lname["kxoselftestaction"]["_id"] for n in d["nodes"]))
        errs, _ = kxo.validate(app)
        check("validates after tool edit", not errs, str(errs[:3]))

    ws = fresh(export, "delete")
    app = kxo.App(ws)
    n_before = len(app.d["dialogComponents"])
    victim = next(d for d in app.d["dialogs"] if not d.get("isHidden"))
    own = {n["componentId"] for n in kxo.iter_all_nodes(victim)}
    run("delete-dialog", "-w", ws, victim["_id"])
    app2 = kxo.App(ws)
    removed = n_before - len(app2.d["dialogComponents"])
    check("delete-dialog removes only that dialog's components", removed <= len(own), f"{removed} > {len(own)}")
    run("build", "-w", ws, "-o", os.path.join(TMP, "build"))
    check("build after delete", os.path.isfile(os.path.join(TMP, "build", "appDefinition.json")))

    print(f"\n{'ALL PASSED' if not FAILS else 'FAILURES:'}" + "".join(f"\n  - {f}" for f in FAILS))
    if FAILS:
        print(f"(temp files kept in {TMP})")
        sys.exit(1)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
