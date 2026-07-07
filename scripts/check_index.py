#!/usr/bin/env python3
"""
Pre-deploy static checks for app/templates/index.html (jstreturn).

Catches the classes of bugs that have broken the page before:

1. JS syntax of the inline function app() { ... } block.
2. Direct property access on nullable state variables
   (tokenInfo.user, user.name, importResult.summary.*) inside
   x-text / x-show / x-bind / x-html / x-if / :class expressions
   without a guard (`x && ...` / `x ? ... : ...`).
3. Alpine `:key="idx"` on x-for loops over arrays that get spliced
   (causes DOM state corruption).

Usage:
    python3 scripts/check_index.py [path/to/index.html]

Exit code 0 = clean. Non-zero = findings.
"""
from __future__ import annotations
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NULLABLE_STATE_PATTERNS = [
    r'^\s*(\w+):\s*null,\s*$',
    # also catch "tokenInfo: null," inside function bodies
]
# Add the known nullable top-level state keys explicitly. The regex above
# only catches top-level initializers; some may be defined deeper.
KNOWN_NULLABLE = {"user", "tokenInfo", "importResult"}

# Alpine directive attributes whose value expression will be evaluated
# even if a parent x-show hides the element.
DIRECTIVES = ("x-text", "x-show", "x-bind", "x-html", "x-if", "x-class",
              "x-transition:enter", "x-transition:leave", ":class", ":style")


def extract_app_block(html: str) -> tuple[int, int]:
    m = re.search(r'function app\(\)\s*\{', html)
    if not m:
        raise SystemExit("could not locate `function app() {` in template")
    start = m.start()
    # find matching closing brace
    depth = 0
    i = m.end() - 1  # position of '{'
    while i < len(html):
        c = html[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    raise SystemExit("could not find closing brace of function app()")


def check_js_syntax(html: str) -> list[str]:
    start, end = extract_app_block(html)
    body = html[start:end]
    body = re.sub(r'\{%.*?%\}', '', body, flags=re.S)  # jinja
    body = re.sub(r'\{\{.*?\}\}', '', body, flags=re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if r.returncode != 0:
        return [f"JS syntax error in function app():\n{r.stderr.strip()}"]
    return []


def find_nullable_keys(html: str) -> set[str]:
    # match `name: null,` style initializers (top of function app() or in
    # object literals). Restrict to known safe subset to avoid false positives.
    found = set()
    for m in re.finditer(r'^\s{4,6}(\w+):\s*null,', html, re.M):
        if m.group(1) in KNOWN_NULLABLE:
            found.add(m.group(1))
    return found


def line_has_guard(line: str, var: str) -> bool:
    """Heuristic: does this line guard against var being null/falsy?"""
    # Patterns that count as a guard:
    #  - `${var} && ...`
    #  - `${var} ? ...`
    #  - `${var}.x && ${var}` (rare)
    if re.search(rf'\b{re.escape(var)}\s*&&\s', line):
        return True
    if re.search(rf'\b{re.escape(var)}\s*\?', line):
        return True
    # also count: expression is wrapped by an outer template x-if with this var
    return False


def check_nullable_access(html: str) -> list[str]:
    nullable = find_nullable_keys(html)
    if not nullable:
        return []
    lines = html.splitlines()
    findings: list[str] = []

    # Build a window map: for each line, is there an enclosing
    # <template x-if="VAR"> that has not yet been closed?
    open_if = []  # stack of (line_no, var)
    for i, line in enumerate(lines):
        for m in re.finditer(r'<template\s+x-if="([^"]+)"', line):
            v = m.group(1).strip()
            if v in nullable:
                open_if.append((i, v))
        # rough close detection: </template> pops one
        if '</template>' in line and open_if:
            open_if.pop()

        for var in nullable:
            # find direct attribute access on nullable in any directive
            for m in re.finditer(
                rf'({"|".join(DIRECTIVES)})="([^"]*\b{re.escape(var)}\.[a-zA-Z_])"',
                line,
            ):
                expr = m.group(2)
                if line_has_guard(line, var):
                    continue
                # if any open enclosing x-if matches this var, safe
                if any(v == var for (_, v) in open_if):
                    continue
                findings.append(
                    f"L{i+1}: {m.group(1)} access on nullable `{var}` "
                    f"without guard: {line.strip()[:160]}"
                )
    return findings


def check_xfor_key(html: str) -> list[str]:
    """Alpine x-for with :key='idx' on arrays that get spliced causes
    DOM state corruption when items are removed/reordered."""
    findings = []
    lines = html.splitlines()
    for i, line in enumerate(lines):
        m = re.search(r'<template\s+x-for="[^"]+"\s+:key="(\w+)"', line)
        if not m:
            continue
        key = m.group(1)
        if key == "idx" or key == "index":
            findings.append(
                f"L{i+1}: x-for uses :key=\"{key}\" — use a stable id from "
                f"the iterated item (e.g. h._id) to avoid DOM corruption on splice/push"
            )
    return findings


def main() -> int:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = Path(__file__).resolve().parent.parent / "app" / "templates" / "index.html"
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2
    html = path.read_text()
    print(f"checking {path}  ({len(html)} chars)")

    findings: list[str] = []
    findings += check_js_syntax(html)
    findings += check_nullable_access(html)
    findings += check_xfor_key(html)

    if not findings:
        print("OK — all checks passed")
        return 0
    print(f"\n{len(findings)} finding(s):", file=sys.stderr)
    for f in findings:
        print(f"  • {f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())