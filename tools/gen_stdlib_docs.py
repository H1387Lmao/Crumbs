#!/usr/bin/env python3
"""Generate docs/stdlib.json (the standard-library reference) from src/std/*.cb.

Every `pub` item is documented by the `//` comment block directly above it. A file's leading comment block is the
module description; its 4-space-indented lines form the module's example. Run:  python3 tools/gen_stdlib_docs.py
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STD = os.path.join(ROOT, "src", "std")
OUT = os.path.join(ROOT, "docs", "stdlib.json")
EXTRA = os.path.join(ROOT, "docs", "stdlib_builtins.json")   # hand-written: the prelude and compiler builtins

GROUPS = [
    ("Text and numbers", ["str", "conv", "fmt", "math", "rand"]),
    ("Collections", ["list", "stack", "queue", "map", "tree", "mem"]),
    ("Input, output and system", ["io", "fs", "env", "time"]),
    ("Networking", ["net", "http"]),
    ("Concurrency", ["thread", "async"]),
]


def strip_comment(line):
    """Code part of a line (// outside of string literals)."""
    in_s = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_s:
            if c == "\\":
                i += 1
            elif c == in_s:
                in_s = None
        elif c in "\"'":
            in_s = c
        elif c == "/" and line[i:i + 2] == "//":
            return line[:i]
        i += 1
    return line


def split_top(text, sep=","):
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            if not (ch == ">" and cur.endswith("-")):
                depth -= 1
        if ch == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def parse_sig(head):
    """head: text after `fn`, up to (not including) the body `{` or `;`."""
    m = re.match(r"\s*([A-Za-z_]\w*)\s*(<[^(]*>)?\s*\(", head)
    if not m:
        return None
    name = m.group(1)
    tparams = split_top(m.group(2)[1:-1]) if m.group(2) else []
    i = m.end()
    depth, start = 1, i
    while i < len(head) and depth:
        if head[i] == "(":
            depth += 1
        elif head[i] == ")":
            depth -= 1
        i += 1
    plist = head[start:i - 1]
    rest = head[i:].strip()
    ret = rest[2:].strip() if rest.startswith("->") else ""
    params, receiver = [], None
    for p in split_top(plist):
        if re.fullmatch(r"(mut\s+)?self(\s*:.*)?", p):
            receiver = "mut self" if p.startswith("mut") else "self"
            continue
        default = None
        if "=" in p and not re.search(r"[=!<>]=", p):
            p, default = [x.strip() for x in p.split("=", 1)]
        nm, _, ty = p.partition(":")
        params.append({"name": nm.strip(), "type": ty.strip(), **({"default": default} if default else {})})
    return {"name": name, "tparams": tparams, "params": params, "returns": ret, "receiver": receiver}


def parse_module(path):
    name = os.path.basename(path)[:-3]
    lines = open(path).read().split("\n")
    i = 0
    header = []
    while i < len(lines) and (lines[i].startswith("//") or not lines[i].strip()):
        if lines[i].startswith("//"):
            header.append(lines[i][2:][1:] if lines[i][2:3] == " " else lines[i][2:])
        elif header:
            break
        i += 1
    # header: prose lines and an indented example
    prose, example = [], []
    for h in header:
        if h.startswith("  ") and h.strip():
            example.append(h)
        elif not h.strip() and example and (len(prose) and prose[-1] == "<ex>"):
            example.append("")
        else:
            prose.append(h)
        if example and (not prose or prose[-1] != "<ex>") and h.startswith("  "):
            prose.append("<ex>")
    prose = [p for p in prose if p != "<ex>"]
    if example:
        ind = min(len(x) - len(x.lstrip()) for x in example if x.strip())
        example = [x[ind:] for x in example]
    summary = prose[0].strip() if prose else ""
    summary = re.sub(r"^std\.\w+:\s*", "", summary)
    desc_lines = [p for p in prose[1:]]
    desc = "\n".join(desc_lines).strip()
    # when the very first block is attached to an item (no blank line before it), it is also that item's doc
    first_attached = i < len(lines) and lines[i].strip() != "" and (i == len(header))
    mod = {"id": name, "name": name, "import": f"std.{name}", "summary": summary, "description": desc,
           "example": "\n".join(example).strip("\n"), "errors": [], "consts": [], "types": [], "traits": [],
           "functions": []}
    doc, depth = [], 0
    ctx = None                      # current impl: {"type":..., "trait":..., "tparams":...}
    ctx_depth = None
    n = len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if s.startswith("//"):
            t = s[2:]
            if re.match(r"\s*-{3,}.*-{3,}\s*$", t) or re.match(r"\s*-{6,}\s*$", t):
                i += 1
                continue
            doc.append(t[1:] if t.startswith(" ") else t)
            i += 1
            continue
        if not s:
            if depth == 0 and not (ctx and ctx_depth is not None):
                doc = [] if not first_attached else doc
            i += 1
            continue
        code = strip_comment(raw)
        text = code.strip()
        # gather multi-line headers (up to `{` or `;`)
        full, j = text, i
        top_level = depth == 0 or (ctx is not None and depth == ctx_depth + 1)
        is_head = top_level and re.match(r"(pub\s+)?(extern\s+)?(fn|struct|enum|error|const|trait|impl|drop)\b", text)
        if is_head:
            while not re.search(r"[{;]", strip_comment(lines[j]).split("->")[-1] if j == i else strip_comment(lines[j])) \
                    and j + 1 < n and ("(" in full and full.count("(") > full.count(")") or not re.search(r"[{;]", full)):
                j += 1
                full += " " + strip_comment(lines[j]).strip()
            if re.match(r"(pub\s+)?(struct|enum|trait)\b", text):
                while full.count("{") > full.count("}") and j + 1 < n:
                    j += 1
                    full += " " + strip_comment(lines[j]).strip()
        pub = bool(re.match(r"pub\b", text))
        docstr = "\n".join(doc).strip()
        if is_head and depth == 0:
            m = re.match(r"(pub\s+)?impl\b(<[^>]*>)?\s*(?:([\w.]+(?:<[^{]*?>)?)\s+for\s+)?([\w.]+(?:<[^{]*?>)?)\s*\{", full)
            if m:
                ctx = {"type": m.group(4), "trait": m.group(3), "tparams": m.group(2)}
                ctx_depth = 0
            elif pub or text.startswith("trait"):
                handle_item(mod, full, pub or True, docstr, None, raw)
        elif is_head and ctx is not None and depth == ctx_depth + 1:
            handle_item(mod, full, pub, docstr, ctx)
        # track braces on the code of the consumed lines
        for k in range(i, j + 1):
            c = strip_comment(lines[k])
            c = re.sub(r"\"(\\.|[^\"\\])*\"|'(\\.|[^'\\])*'", "", c)
            depth += c.count("{") - c.count("}")
        if ctx is not None and depth <= ctx_depth:
            ctx, ctx_depth = None, None
        doc = []
        first_attached = False
        i = j + 1
    return mod


def handle_item(mod, full, pub, docstr, ctx, raw_line=None):
    text = full.strip()
    if ctx is None and not pub and not text.startswith("trait"):
        return
    pm = re.match(r"pub\s+(.*)", text)
    body = pm.group(1) if pm else text
    if ctx is None and body.startswith("error "):
        trail = ""
        m = re.search(r"//\s*(.*)$", raw_line or "")
        if m and not docstr:
            trail = m.group(1)
        for en in body[6:].strip().rstrip(";").split(","):
            mod["errors"].append({"name": en.strip(), "doc": docstr or trail})
    elif ctx is None and body.startswith("const "):
        m = re.match(r"const\s+(\w+)\s*:\s*([^=]+)=\s*([^;]+);", body)
        if m:
            mod["consts"].append({"name": m.group(1), "type": m.group(2).strip(), "value": m.group(3).strip(), "doc": docstr})
    elif ctx is None and (body.startswith("struct ") or body.startswith("enum ")):
        m = re.match(r"(struct|enum)\s+(\w+)\s*(<[^{]*>)?\s*(\{.*\})?", body)
        kind, nm = m.group(1), m.group(2)
        tp = split_top(m.group(3)[1:-1]) if m.group(3) else []
        fields = []
        if m.group(4) and kind == "struct":
            inner = m.group(4)[1:]
            inner = inner[:inner.rfind("}")] if "}" in inner else inner
            for f in split_top(inner):
                if ":" in f:
                    a, _, b = f.partition(":")
                    fields.append({"name": a.strip(), "type": b.strip()})
        elif m.group(4):
            inner = m.group(4)[1:]
            inner = inner[:inner.rfind("}")] if "}" in inner else inner
            fields = [{"name": v.strip()} for v in split_top(inner)]
        mod["types"].append({"name": nm, "kind": kind, "tparams": tp, "doc": docstr, "fields": fields, "methods": []})
    elif ctx is None and body.startswith("trait "):
        m = re.match(r"trait\s+(\w+)\s*(?:\{(.*)\})?", body)
        methods = []
        for part in (m.group(2) or "").split(";"):
            part = part.strip()
            if part.startswith("fn "):
                sig = parse_sig(part[3:])
                if sig:
                    sig["doc"] = ""
                    sig["fallible"] = sig["returns"].endswith("!")
                    methods.append(sig)
        mod["traits"].append({"name": m.group(1), "doc": docstr, "methods": methods})
    elif re.match(r"(extern\s+)?fn\b", body) and (pub or ctx is not None):
        body = re.sub(r"^extern\s+", "", body)
        head = re.sub(r"\{.*$", "", body[2:]).rstrip(" ;")
        sig = parse_sig(head)
        if not sig:
            return
        sig["doc"] = docstr
        fallible = sig["returns"].endswith("!")
        sig["fallible"] = fallible
        if ctx is None:
            mod["functions"].append(sig)
        else:
            base = re.sub(r"<.*", "", ctx["type"])
            if ctx.get("trait"):
                sig["via"] = ctx["trait"]
            owner = next((t for t in mod["types"] if t["name"] == base), None)
            if owner is None:
                owner = {"name": base, "kind": "builtin", "tparams": [], "doc": "", "fields": [], "methods": [], "ext": True}
                mod["types"].append(owner)
            sig["tparams"] = sig["tparams"]
            if ctx.get("tparams"):
                sig["impl_tparams"] = split_top(ctx["tparams"][1:-1])
            owner["methods"].append(sig)


def load_notes():
    notes = {}
    path = os.path.join(ROOT, "tools", "stdlib_notes.txt")
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(" :: ")]
        parts += [""] * (4 - len(parts))
        key, doc, ret, params = parts[:4]
        pn = {}
        for item in params.split(";"):
            if "=" in item:
                a, _, b = item.partition("=")
                pn[a.strip()] = b.strip()
        notes[key] = {"doc": doc, "returns_doc": ret.rstrip(":").strip(), "params": pn}
    return notes


def apply_notes(mods, notes):
    used = set()
    def fill(f, key):
        n = notes.get(key)
        if not n:
            return
        used.add(key)
        if not f.get("doc"):
            f["doc"] = n["doc"]
        if n["returns_doc"] and not f.get("returns_doc"):
            f["returns_doc"] = n["returns_doc"]
        for p in f["params"]:
            if p["name"] in n["params"]:
                p["doc"] = n["params"][p["name"]]
    for m in mods.values():
        for f in m["functions"]:
            fill(f, f"{m['name']}.{f['name']}")
        for t in m["types"]:
            for f in t["methods"]:
                fill(f, f"{m['name']}.{t['name']}.{f['name']}")
    return [k for k in notes if k not in used]


def apply_meta(mods):
    path = os.path.join(ROOT, "tools", "stdlib_meta.json")
    meta = json.load(open(path))
    for mid, mm in meta.items():
        m = mods.get(mid)
        if not m:
            print("meta for unknown module", mid)
            continue
        for k in ("summary", "description"):
            if mm.get(k) and not m.get(k):
                m[k] = mm[k]
        if mm.get("example"):                      # meta examples are compile-checked: they win over header prose
            m["example"] = mm["example"]
        for k, v in mm.get("errors", {}).items():
            for e in m["errors"]:
                if e["name"] == k and not e["doc"]:
                    e["doc"] = v
        for k, v in mm.get("consts", {}).items():
            for e in m["consts"]:
                if e["name"] == k and not e["doc"]:
                    e["doc"] = v
        for k, v in mm.get("types", {}).items():
            for t in m["types"]:
                if t["name"] == k:
                    if isinstance(v, str):
                        v = {"doc": v}
                    if v.get("doc") and (not t["doc"] or t["doc"].startswith("-")):
                        t["doc"] = v["doc"]
                    if v.get("show_fields"):
                        t["show_fields"] = True
                    for fk, fv in v.get("fields", {}).items():
                        for f in t["fields"]:
                            if f["name"] == fk:
                                f["doc"] = fv
        for k, v in mm.get("traits", {}).items():
            for t in m["traits"]:
                if t["name"] == k:
                    if v.get("doc") and not t["doc"]:
                        t["doc"] = v["doc"]
                    for mk, mv in v.get("methods", {}).items():
                        for f in t["methods"]:
                            if f["name"] == mk and not f.get("doc"):
                                f["doc"] = mv
                    for pk, pv in v.get("returns", {}).items():
                        for f in t["methods"]:
                            if f["name"] == pk:
                                f["returns_doc"] = pv
        for k in mm.get("hide_types", []):
            m["types"] = [t for t in m["types"] if t["name"] != k]


def main():
    mods = {}
    for fn in sorted(os.listdir(STD)):
        if fn.endswith(".cb") and not fn.startswith("_"):
            m = parse_module(os.path.join(STD, fn))
            mods[m["name"]] = m
    apply_meta(mods)
    stale = apply_notes(mods, load_notes())
    if stale:
        print("notes for items that do not exist:", ", ".join(stale))
    extra = json.load(open(EXTRA)) if os.path.exists(EXTRA) else {"modules": []}
    groups = []
    seen = set()
    if extra.get("modules"):
        groups.append({"title": "Prelude and builtins", "modules": [m["id"] for m in extra["modules"]]})
        for m in extra["modules"]:
            mods[m["id"]] = m
    for title, names in GROUPS:
        groups.append({"title": title, "modules": [n for n in names if n in mods]})
        seen.update(names)
    rest = [n for n in mods if n not in seen and n not in {m["id"] for m in extra.get("modules", [])}]
    if rest:
        groups.append({"title": "Other", "modules": rest})
    for m in mods.values():                   # drop empty groups of keys for compactness
        for k in ("errors", "consts", "types", "traits", "functions"):
            m.setdefault(k, [])
    out = {"groups": groups, "modules": [mods[n] for g in groups for n in g["modules"]]}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    missing = []
    for m in out["modules"]:
        for fnc in m["functions"]:
            if not fnc.get("doc"):
                missing.append(f"{m['name']}.{fnc['name']}")
        for t in m["types"]:
            for me in t["methods"]:
                if not me.get("doc") and not me.get("via"):
                    missing.append(f"{m['name']}.{t['name']}.{me['name']}")
    print(f"wrote {OUT}: {len(out['modules'])} modules, {sum(len(m['functions']) + sum(len(t['methods']) for t in m['types']) for m in out['modules'])} functions")
    if "--missing" in sys.argv:
        print("undocumented:", len(missing)); print("\n".join(missing))


if __name__ == "__main__":
    main()
