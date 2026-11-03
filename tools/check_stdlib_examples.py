#!/usr/bin/env python3
"""Compile every example in docs/stdlib.json, so the reference never shows code that does not build."""
import json, os, re, subprocess, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data = json.load(open(os.path.join(ROOT, "docs", "stdlib.json")))
bad = 0
for m in data["modules"]:
    ex = m.get("example")
    if not ex:
        continue
    lines = ex.split("\n")
    top, body, depth, in_item = [], [], 0, False
    for l in lines:
        starts = bool(re.match(r"(import|pub |struct |impl |trait |fn |async fn |error |const )", l))
        if depth == 0 and (starts or in_item) and not l.startswith(" "):
            in_item = starts and not l.rstrip().endswith(";")
        if in_item or (starts and depth == 0):
            top.append(l)
        else:
            body.append(l)
        depth += l.count("{") - l.count("}")
        if depth == 0:
            in_item = False
    if "fn main" in ex:
        src = ex
    else:
        src = "\n".join(top) + "\nfn main() {\n" + "\n".join("    " + l for l in body) + "\n}\n"
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ex.cb")
        open(p, "w").write(src)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "crumbs.py"), "build", p], capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            bad += 1
            print(f"--- {m['id']} example does not compile\n{r.stderr or r.stdout}"[:1500])
        else:
            print(f"ok  {m['id']}")
sys.exit(1 if bad else 0)
