"""Module loading: resolves `import a.b;` to files, detects cycles, orders modules."""
import os
from .diag import Source, CompileError
from .parser import parse

STD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "std")


class Import:
    def __init__(self, mod, node):
        self.mod = mod
        self.node = node
        self.used = False


class Module:
    def __init__(self, logical, path, src, ast, cprefix):
        self.name = logical          # dotted logical name, e.g. std.math
        self.path = path
        self.src = src
        self.ast = ast
        self.cprefix = cprefix
        self.imports = {}            # alias -> Import
        self.syms = {}               # name -> Sym
        self.is_entry = False
        self.is_std = False
        self.directives = []         # (kind, value, span) from link / include / define

    def __repr__(self):
        return f"<module {self.name}>"


class Loader:
    def __init__(self, diags, include_dirs=(), nostd=False):
        self.d = diags
        self.nostd = nostd
        self.include_dirs = list(include_dirs)
        self.by_path = {}
        self.order = []
        self.used_prefixes = set()
        self.root = None

    def prefix_for(self, logical):
        base = logical.replace(".", "__")
        p, k = base, 1
        while p in self.used_prefixes:
            k += 1
            p = f"{base}{k}"
        self.used_prefixes.add(p)
        return p

    def load_entry(self, path):
        path = os.path.abspath(path)
        self.root = os.path.dirname(path)
        if not self.nostd:          # the prelude: `List` and the `String` / `str` methods need no import
            for name in ("list", "str"):
                self._load(os.path.join(STD_DIR, name + ".cb"), "std." + name, [], None)
        stem = os.path.splitext(os.path.basename(path))[0]
        m = self._load(path, stem, [], display=None)
        m.is_entry = True
        return self.order

    def _read(self, path, display):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise CompileError(f"cannot read `{path}`: {e.strerror}")
        except UnicodeDecodeError:
            raise CompileError(f"`{path}` is not valid UTF-8")
        shown = display or os.path.relpath(path)
        return Source(shown, text)

    def _load(self, path, logical, stack, display):
        if path in self.by_path:
            return self.by_path[path]
        src = self._read(path, display)
        ast = parse(src, self.d)
        m = Module(logical, path, src, ast, self.prefix_for(logical))
        m.is_std = os.path.commonpath([path, STD_DIR]) == STD_DIR
        self.by_path[path] = m
        self._directives(m)
        stack = stack + [m]
        for it in ast.items:
            if it.kind != "Import":
                continue
            alias = it.alias or it.path[-1]
            dotted = ".".join(it.path)
            target = self._resolve(it, m)
            if target is None:
                continue
            if target in [s.path for s in stack]:
                chain = " -> ".join([s.name for s in stack] + [dotted])
                self.d.error(it.span, f"circular import of module `{dotted}`", label="imports a module that imports this one",
                             notes=[f"import chain: {chain}"])
                continue
            dep = self._load(target, dotted, stack, None)
            if alias in m.imports:
                self.d.error(it.name_span, f"module name `{alias}` is imported more than once",
                             help="use `import ... as other_name;` to disambiguate")
                continue
            m.imports[alias] = Import(dep, it)
        self.order.append(m)
        return m

    FILE_EXTS = (".o", ".obj", ".a", ".so", ".dylib", ".dll", ".lib", ".c")

    def _directives(self, m):
        """Collect `link` / `include` / `define` directives; resolve linked files relative to the module."""
        here = os.path.dirname(m.path)
        for it in m.ast.items:
            if it.kind != "Directive":
                continue
            what, val = it.what, it.value
            if not val.strip() or "\n" in val or "\0" in val:
                self.d.error(it.vspan, f"invalid `{it.what}` string")
                continue
            if what == "link":
                if os.sep in val or "/" in val or val.lower().endswith(self.FILE_EXTS):
                    full = val if os.path.isabs(val) else os.path.join(here, val)
                    if not os.path.isfile(full):
                        self.d.error(it.vspan, f"linked file `{val}` not found",
                                     notes=[f"looked for: {os.path.relpath(full)}"],
                                     help="paths are relative to the file that contains the `link` directive; "
                                          "write a bare name like `link \"SDL2\";` to link a library with -l")
                        continue
                    m.directives.append(("file", os.path.abspath(full), it.span))
                else:
                    m.directives.append(("lib", val, it.span))
            elif what == "pkg":
                m.directives.append(("pkg", val, it.span))
            elif what == "include":
                m.directives.append(("include", val, it.span))
                m.directives.append(("incdir", here, it.span))
            elif what == "define":
                m.directives.append(("define", val, it.span))

    def _resolve(self, it, m):
        rel = os.path.join(*it.path) + ".cb"
        dotted = ".".join(it.path)
        roots = []
        if it.path[0] == "std":
            if self.nostd and dotted != "std.mem":
                self.d.error(it.span, f"`{dotted}` needs the standard runtime, which `--no-stdruntime` removes",
                             label="not available in this build",
                             help="only `std.mem` works without it; drop --no-stdruntime or bind the C functions you need with `extern fn`")
                return None
            roots.append(STD_DIR)
            rel = os.path.join(*it.path[1:]) + ".cb" if len(it.path) > 1 else rel
        else:
            roots.append(self.root)
            roots.append(os.path.dirname(m.path))
            roots.extend(self.include_dirs)
        tried = []
        for r in roots:
            cand = os.path.join(r, rel)
            tried.append(os.path.relpath(cand))
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        self.d.error(it.span, f"cannot find module `{dotted}`", label="no such module",
                     notes=["looked for: " + ", ".join(dict.fromkeys(tried))])
        return None
