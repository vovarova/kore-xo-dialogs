#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keep the agent out of env/ (values per environment) and
workspace/import-package/ (built zips with real values).

Blocks a tool call that names those folders, or a recursive shell search/copy over the project that could read
them without naming them. `kore` reads them itself, so the agent never needs to.
"""
import json
import re
import sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
    tool = data.get("tool_name", "")
    text = json.dumps(data.get("tool_input", {}))
except (ValueError, AttributeError):
    tool, text = "", raw  # unparsable input: still scan it (fail closed)
low = text.lower().replace("\\\\", "/").replace("\\", "/")


def block(why):
    print(f"Blocked: {why} env/ and workspace/import-package/ hold real values and are off-limits to the agent. "
          "Use `kore build --env <name>`; if a value must change, ask the user to edit the env file.",
          file=sys.stderr)
    sys.exit(2)


if "import-package" in low or re.search(r"(^|[\s\"'/=:])env/|\"(?:[^\"]*/)?env\"|(^|[\s\"'/])env\*", low):
    block("this call refers to a protected folder.")

if tool in ("Bash", "PowerShell") or not tool:
    if re.search(
            r"(\bgrep\b[^|;&\"]*\s-[a-z]*r|\bgrep\b[^|;&]*--recursive|\begrep\b[^|;&]*\s-[a-z]*r|"
            r"\bfindstr\b[^|;&]*/s\b|\bselect-string\b[^|;&]*-recurse|"
            r"\b(get-childitem|gci|ls|dir)\b[^|;&]*-recurse|\bdir\b[^|;&]*/s\b|\bls\s+-[a-z]*r|"
            r"\bfind\s+(\.|/|[a-z]:|~)|(?:\"|[;&|]\s*)(tar|zip|7z|robocopy|xcopy)\s|"
            r"\bcopy-item\b[^|;&]*-recurse|\bcp\s+-[a-z]*r)", low):
        block("recursive shell search/copy could read a protected folder. Use the Grep/Glob tools (they skip it) "
              "or name the subfolders you need (dialogs/, specs/, workspace/).")
sys.exit(0)
