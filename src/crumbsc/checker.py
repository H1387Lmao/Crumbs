"""Semantic analysis: name resolution, type checking, mutability, control-flow checks.

The checker annotates the AST in place (node.ty, node.local, node.sym, ...) so the
code generator never has to re-derive types.
"""
import copy
import re
from .timing import TIMINGS
from .diag import suggest
from .parser import N, Parser
from .borrowck import BorrowCk
from .types import (clo as clo_ty, INTS, PRIMS, Ty, I8, I16, I32, I64, U8, U16, U32, U64, F32, F64, BOOL, STR, VOID,
                    ERR, POISON, STRING, REGION, ptr, arr, slc, eu, opt, fnty, same)

BUILTINS = {"print", "println", "format", "panic", "assert", "alloc", "free", "exit", "to_str",
            "string_new", "string_from", "string_clear",
            "cstr", "from_cstr", "slice_of", "size_of", "drop", "drop_at", "write_at", "read_at",
            "gc_collect", "gc_bytes", "gc_collections", "alloc_in",
            "gc_collect_gen", "gc_gen_bytes", "gc_gen_collections"}
ARITH = {"+", "-", "*", "/", "%"}
BITWISE = {"&", "|", "^"}
SHIFT = {"<<", ">>"}
CMP = {"==", "!=", "<", ">", "<=", ">="}
RESERVED_NAMES = set(PRIMS) | BUILTINS | {"error", "null"}


class NotConst(Exception):
    def __init__(self, span, msg):
        self.span = span
        self.msg = msg


class Sym:
    def __init__(self, kind, name, mod, node, pub):
        self.kind = kind            # fn struct enum const error
        self.name = name
        self.mod = mod
        self.node = node
        self.pub = pub
        self.full = f"{mod.cprefix}.{name}"
        self.cname = f"{mod.cprefix}__{name}"
        self.ty = None
        self.params = []
        self.ret = VOID
        self.extern = False
        self.fields = []
        self.field_map = {}
        self.variants = []
        self.code = 0
        self.value = None
        self.evaluating = False
        self.evaluated = False
        self.tparams = []
        self.base = None      # generic template this instance came from
        self.targs = []
        self.tenv = {}
        self.export = False
        self.variadic = False
        self.csym = None      # for extern fns / consts: the real C symbol or expression
        self.extern_struct = False
        self.opaque = False
        self.is_method = False
        self.has_self = False
        self.self_mut = False
        self.drop_sym = None  # struct: its destructor (generic template for generic structs)
        self.drop_inst = None # struct instance: the instantiated destructor
        self.is_drop = False


class Local:
    _n = 0

    def __init__(self, name, ty, mut, cname, span, is_param=False):
        Local._n += 1
        self.id = Local._n
        self.glue = False
        self.name = name
        self.ty = ty
        self.mut = mut
        self.cname = cname
        self.span = span
        self.used = False
        self.is_param = is_param
        self.rdepth = 0           # how many `region` blocks enclose its declaration
        self.taint = None         # the region (a handle Local) whose memory this variable may point into
        self.region_handle = False
        self.captured = False     # a proxy for a variable of an enclosing function, used inside a closure body
        self.byref = False
        self.written = False
        self.outer = None


class BckEntry:
    """A checked function body waiting for the borrow checker (which runs after everything is checked, to use call summaries)."""

    def __init__(self, sym, body, params, context):
        self.sym, self.body, self.params, self.context = sym, body, list(params), context


class ClosureInfo:
    """One closure expression: the variables it captures (outer Local -> the proxy Local used inside its body)."""

    def __init__(self, move, span):
        self.move = move
        self.span = span
        self.caps = {}
        self.sym = None

    def capture(self, loc):
        p = self.caps.get(loc)
        if p is None:
            idx = len(self.caps)
            byref = not self.move
            cname = f"(*cb_e->c{idx})" if byref else f"(cb_e->c{idx})"
            p = Local(loc.name, loc.ty, loc.mut, cname, loc.span)
            p.used = True
            p.captured = True
            p.byref = byref
            p.written = False
            p.outer = loc
            p.region_handle = loc.region_handle
            p.taint = loc.taint
            loc.used = True
            self.caps[loc] = p
        return p


class ClosureScope(dict):
    """The parameter scope of a closure body: looking a name up through it captures the variable."""

    def __init__(self, info):
        super().__init__()
        self.clo = info


class FnInfo:
    def __init__(self, sym):
        self.sym = sym
        self.ret = sym.ret if sym else VOID
        self.names = set()
        self.loops = 0
        self.rdepth = 0          # nesting depth of `region` blocks
        self.try_stack = []      # open `try { }` blocks: frames {"fails": [moved-state at each failure point]}
        self.loop_stack = []     # frames {"breaks": [...], "conts": [...]} for move analysis


class Checker:
    def __init__(self, mods, diags, c_reserved=(), strict=False, entry_name="main", nostd=False, gc_mode="auto", rc=False):
        self.strict = strict
        self.rc = rc                       # --rc: raw pointers and slices are reference counted (copyable, freed with the last owner)
        self.gc_mode = gc_mode             # auto | thread | manual (or "off" with --no-gc)
        self.nostd = nostd                 # --no-stdruntime: no libc-backed features
        self.entry_name = entry_name       # "" = no entry point (library / object-file mode)
        self.mods = mods
        self.d = diags
        self.c_reserved = set(c_reserved)
        self.errors = []     # error syms; code = index + 1
        self.fns = []
        self.structs = []
        self.enums = []
        self.consts = []
        self.m = None
        self.fn = None
        self.scopes = []
        self.entry = mods[-1] if mods else None
        self.main_sym = None
        self.tenv = {}        # type parameter bindings while checking a generic instance
        self.inst_cache = {}
        self.pending = []
        self.bck_entries = []
        self.opt_consumed = set()   # `x.unwrap()` receivers already moved (a call can be checked more than once)
        self.n_inst = 0
        self.glue = {}
        self.glue_busy = set()
        self.glue_nl = {}
        self.methods = {}        # type key -> {method name: Sym}
        self.traits = []         # trait syms
        self.trait_impls = {}    # (trait full name, type key) -> TraitImpl
        self.bound_seen = set()
        self.prelude_types = {}  # names usable without an import (`List`)
        self.prelude_traits = {}  # ... and traits (`Display`)
        self.moved = {}          # Local -> span where it was moved (flow-sensitive)
        self.reinit_ok = None

    # ================================================================ driver
    def run(self):
        with TIMINGS.phase("check"):
            return self._run()

    def _run(self):
        for m in self.mods:
            self.declare(m)
            if m.name == "std.list" and m.is_std and not self.nostd and "List" in m.syms:
                self.prelude_types["List"] = m.syms["List"]
            if m.name == "std.iter" and m.is_std and not self.nostd:
                for tn in ("Iter", "Pair", "Indexed"):
                    if tn in m.syms:
                        self.prelude_types[tn] = m.syms[tn]
            if m.name == "std.fmt" and m.is_std and not self.nostd and "Display" in m.syms:
                self.prelude_traits["Display"] = m.syms["Display"]
        for m in self.mods:
            self.resolve_bounds(m)
        self.check_trait_impls()
        for m in self.mods:
            self.resolve_sigs(m)
        self.check_ffi()
        for m in self.mods:
            self.check_consts(m)
        for m in self.mods:
            self.check_bodies(m)
        while self.pending:
            self.check_fn_body(self.pending.pop(0))
        if self.bck_entries and self.d.error_count == 0:
            BorrowCk(self).run_all(self.bck_entries)
        self.check_struct_cycles()
        for m in self.mods:
            for alias, imp in m.imports.items():
                if not imp.used:
                    self.d.warn(imp.node.span, f"unused import `{alias}`", help="remove it, or use `_`-free names from it")
        self.check_main()
        return self

    # ============================================================ declaration
    def define(self, m, sym, span):
        if sym.name in m.syms:
            old = m.syms[sym.name]
            self.d.error(span, f"the name `{sym.name}` is defined multiple times",
                         label="redefined here",
                         notes=[f"previous definition of `{sym.name}` is at line {old.node.span.line}"])
            return None
        if sym.name in RESERVED_NAMES:
            self.d.error(span, f"`{sym.name}` is a reserved name and cannot be redefined")
            return None
        if sym.name in m.imports:
            self.d.error(span, f"`{sym.name}` conflicts with an imported module of the same name")
            return None
        m.syms[sym.name] = sym
        return sym

    def declare(self, m):
        items = [i for i in m.ast.items if not (i.kind == "Fn" and i.drop_for)]
        drops = [i for i in m.ast.items if i.kind == "Fn" and i.drop_for]
        for it in items:
            k = it.kind
            if k == "Fn":
                s = Sym("fn", it.name, m, it, it.pub)
                s.extern = it.extern
                s.tparams = it.tparams
                s.variadic = it.variadic
                s.export = it.export
                if it.extern:
                    s.csym = it.cname_str or it.name
                    # every binding (std included) gets a private prototype bound to the real symbol
                    # so it can never clash with a C header
                    s.cname = f"cb_x_{m.cprefix}__{it.name}"
                elif it.export:
                    s.cname = it.name
                if self.define(m, s, it.span) and not s.tparams:
                    self.fns.append(s)
            elif k == "Struct":
                s = Sym("struct", it.name, m, it, it.pub)
                s.ty = Ty("struct", sym=s)
                s.tparams = it.tparams
                s.extern_struct = it.extern
                s.opaque = it.opaque
                if self.define(m, s, it.span) and not s.tparams:
                    self.structs.append(s)
            elif k == "Enum":
                s = Sym("enum", it.name, m, it, it.pub)
                s.ty = Ty("enum", sym=s)
                s.variants = [v for v, _ in it.variants]
                if not it.variants:
                    self.d.error(it.span, f"enum `{it.name}` has no variants")
                seen = {}
                for v, sp in it.variants:
                    if v in seen:
                        self.d.error(sp, f"duplicate variant `{v}` in enum `{it.name}`")
                    seen[v] = sp
                if self.define(m, s, it.span):
                    self.enums.append(s)
            elif k == "Trait":
                s = Sym("trait", it.name, m, it, it.pub)
                seen = {}
                for f in it.methods:
                    if f.name in seen:
                        self.d.error(f.span, f"the trait `{it.name}` declares the method `{f.name}` more than once",
                                     notes=[f"the first one is at line {seen[f.name].span.line}"])
                    seen[f.name] = f
                if not it.methods:
                    self.d.error(it.span, f"trait `{it.name}` has no methods", help="declare at least one `fn` inside it")
                s.trait_methods = seen
                s.supers = []
                if self.define(m, s, it.span):
                    self.traits.append(s)
            elif k == "Const":
                s = Sym("const", it.name, m, it, it.pub)
                if it.extern:
                    s.extern = True
                    s.csym = it.cname_str or it.name
                if self.define(m, s, it.span):
                    self.consts.append(s)
            elif k == "ErrorDecl":
                for nm, sp in it.names:
                    s = Sym("error", nm, m, it, it.pub)
                    s.ty = ERR
                    s.node_span = sp
                    if self.define(m, s, sp):
                        self.errors.append(s)
                        s.code = len(self.errors)
        for it in drops:
            self.declare_drop(m, it)
        for it in m.ast.items:
            if it.kind == "Impl":
                self.declare_impl(m, it)

    # ---------------------------------------------------------------- methods
    def type_key(self, t):
        """The key under which methods of the type `t` are registered (None if the type has none)."""
        if t.kind == "prim":
            return t.name
        if t.kind in ("struct", "enum"):
            sym = t.sym.base if getattr(t.sym, "base", None) is not None else t.sym
            return sym.full
        return None

    def declare_impl(self, m, it):
        tyn = it.tyn
        path = tyn.path
        sym = None
        key = None
        if len(path) == 1 and path[0] in PRIMS and path[0] != "void":
            key = path[0]
            if tyn.args or it.tparams:
                self.d.error(tyn.span, f"`{key}` is not generic, so its `impl` takes no type parameters")
                return
        else:
            if len(path) == 1:
                sym = m.syms.get(path[0]) or self.prelude_types.get(path[0])
            elif len(path) == 2 and path[0] in m.imports:
                m.imports[path[0]].used = True
                sym = m.imports[path[0]].mod.syms.get(path[1])
            if sym is None or sym.kind not in ("struct", "enum"):
                self.d.error(tyn.span, f"cannot find the type `{'.'.join(path)}` to attach methods to",
                             label="not a struct, enum or built-in type")
                return
            if sym.kind == "struct" and sym.opaque:
                self.d.error(tyn.span, f"`{sym.name}` is an opaque C type and cannot have methods",
                             help="wrap it in a Crumbs struct and put the methods there")
                return
            key = sym.full
            ok = len(tyn.args) == len(getattr(sym, "tparams", [])) == len(it.tparams) and all(
                a.kind == "TName" and len(a.path) == 1 and not a.args and a.path[0] == tp
                for a, tp in zip(tyn.args, it.tparams))
            if not ok:
                want = f"<{', '.join(sym.tparams)}>" if sym.tparams else ""
                self.d.error(tyn.span, f"an `impl` for `{sym.name}` must be generic over exactly its type parameters"
                             if sym.tparams else f"`{sym.name}` is not generic, so its `impl` takes no type parameters",
                             help=f"write `impl{want} {sym.name}{want} {{ ... }}`")
                return
        table = self.methods.setdefault(key, {})
        tname = path[-1]
        fns = list(it.fns)
        trait = None
        if getattr(it, "trait", None) is not None:
            trait = self.resolve_trait(it.trait, m)
            if trait is None:
                return
            fns = self.conform(m, it, trait, key, tname, fns)
            if fns is None:
                return
        for fn in fns:
            if fn.name in table:
                old = table[fn.name]
                self.d.error(fn.span, f"`{tname}` already has a method named `{fn.name}`",
                             notes=[f"the first one is at line {old.node.span.line}"])
                continue
            node = copy.copy(fn)
            node.name = f"{tname}.{fn.name}"
            node.tparams = list(it.tparams) + list(fn.tparams)
            node.tbounds = {**(getattr(it, "tbounds", None) or {}), **(getattr(fn, "tbounds", None) or {})}
            s = Sym("fn", node.name, m, node, fn.pub or trait is not None)
            s.tparams = node.tparams
            s.cname = f"{m.cprefix}__{tname}__m_{fn.name}"
            s.is_method = True
            s.method_name = fn.name
            s.has_self = bool(fn.params) and fn.params[0].name == "self"
            s.self_mut = s.has_self and getattr(fn.params[0], "self_mut", False)
            s.self_mut = s.has_self and (getattr(fn.params[0], "self_mut", False) or getattr(fn.params[0], "mut", False))
            if self.define(m, s, fn.span):
                table[fn.name] = s
                if not s.tparams:
                    self.fns.append(s)

    # ---------------------------------------------------------------- traits
    def resolve_trait(self, tn, m, quiet=False):
        """The trait symbol a path like `Shape` / `shapes.Shape` names (reports an error and returns None otherwise)."""
        path = tn.path
        sym = None
        if not quiet:
            if id(tn) in self.bound_seen:       # the same bound node is shared by an impl and each of its methods
                quiet = True
            self.bound_seen.add(id(tn))
        if len(path) == 1:
            sym = m.syms.get(path[0])
            if sym is None and path[0] not in m.imports:
                sym = self.prelude_traits.get(path[0])
        elif len(path) == 2:
            imp = m.imports.get(path[0])
            if imp is None:
                if not quiet:
                    self.d.error(tn.span, f"cannot find module `{path[0]}`", help=f"add `import {path[0]};` at the top of the file")
                return None
            imp.used = True
            sym = imp.mod.syms.get(path[1])
            if sym is not None and sym.kind == "trait" and not sym.pub and sym.mod is not m:
                if not quiet:
                    self.d.error(tn.span, f"trait `{path[1]}` is private to module `{path[0]}`", help="mark it `pub` in its module")
                return None
        if sym is not None and sym.kind == "trait":
            return sym
        if quiet:
            return None
        name = ".".join(path)
        if sym is not None:
            self.d.error(tn.span, f"`{name}` is a {sym.kind}, not a trait", label="expected a trait here")
            return None
        scope = m.syms if len(path) == 1 else m.imports[path[0]].mod.syms
        sg = suggest(path[-1], [n for n, v in scope.items() if v.kind == "trait"])
        self.d.error(tn.span, f"cannot find trait `{name}` in this scope", label="not found",
                     help=f"a trait with a similar name exists: `{sg}`" if sg else None)
        return None

    def canon(self, tn, m, selfc):
        """A canonical string for a type AST: names are replaced by their full symbol names, `Self` by `selfc`.
        Two signatures that spell the same types differently (`Point` / `geom.Point`) get the same string."""
        k = tn.kind
        if k == "TName":
            p = tn.path
            if p == ["Self"] and not tn.args:
                return selfc
            if len(p) == 1:
                sym = None if p[0] in PRIMS else (m.syms.get(p[0]) or self.prelude_types.get(p[0]))
                base = sym.full if sym is not None else p[0]
            elif len(p) == 2 and p[0] in m.imports:
                sym = m.imports[p[0]].mod.syms.get(p[1])
                base = sym.full if sym is not None else ".".join(p)
            else:
                base = ".".join(p)
            if tn.args:
                base += "<" + ",".join(self.canon(a, m, selfc) for a in tn.args) + ">"
            return base
        if k == "TPtr":
            return ("*const " if getattr(tn, "ro", False) else "*") + self.canon(tn.inner, m, selfc)
        if k == "TSlice":
            return "[]" + self.canon(tn.inner, m, selfc)
        if k == "TArray":
            return f"[{getattr(tn.size, 'value', '?')}]" + self.canon(tn.inner, m, selfc)
        if k == "TErrUnion":
            return self.canon(tn.inner, m, selfc) + "!"
        if k == "TOpt":
            return self.canon(tn.inner, m, selfc) + "?"
        if k == "TFn":
            ret = self.canon(tn.ret, m, selfc) if tn.ret is not None else "void"
            return "fn(" + ",".join(self.canon(x, m, selfc) for x in tn.params) + ")->" + ret
        if k == "TClo":
            ret = self.canon(tn.ret, m, selfc) if tn.ret is not None else "void"
            return "clo(" + ",".join(self.canon(x, m, selfc) for x in tn.params) + ")->" + ret
        return "?"

    def tn_text(self, tn):
        k = tn.kind
        if k == "TName":
            return ".".join(tn.path) + (f"<{', '.join(self.tn_text(a) for a in tn.args)}>" if tn.args else "")
        if k == "TPtr":
            return ("*const " if getattr(tn, "ro", False) else "*") + self.tn_text(tn.inner)
        if k == "TSlice":
            return "[]" + self.tn_text(tn.inner)
        if k == "TArray":
            return f"[{getattr(tn.size, 'value', '..')}]" + self.tn_text(tn.inner)
        if k == "TErrUnion":
            return self.tn_text(tn.inner) + "!"
        if k == "TOpt":
            return self.tn_text(tn.inner) + "?"
        if k == "TFn":
            return f"fn({', '.join(self.tn_text(x) for x in tn.params)})" + (f" -> {self.tn_text(tn.ret)}" if tn.ret is not None else "")
        if k == "TClo":
            return f"|{', '.join(self.tn_text(x) for x in tn.params)}|" + (f" -> {self.tn_text(tn.ret)}" if tn.ret is not None else "")
        return "?"

    def sig_text(self, fn):
        ps = ", ".join(("mut self" if getattr(p, "self_mut", False) else "self") if getattr(p, "self_param", False)
                       else f"{p.name}: {self.tn_text(p.tyn)}" for p in fn.params)
        return f"fn {fn.name}({ps})" + (f" -> {self.tn_text(fn.ret)}" if fn.ret is not None else "")

    def conform(self, m, it, trait, key, tname, fns):
        """Check an `impl Trait for Type` block against the trait. Returns the list of functions to register
        (the impl's own plus the trait's default methods it does not override), or None after an error."""
        tm = trait.mod
        ikey = (trait.full, key)
        shown = f"{tname}<{', '.join(it.tparams)}>" if it.tparams else tname
        if ikey in self.trait_impls:
            old = self.trait_impls[ikey]
            self.d.error(it.span, f"`{shown}` already implements the trait `{trait.name}`",
                         label="conflicting implementation",
                         notes=[f"the first `impl` is at line {old.node.span.line}"])
            return None
        strays = []
        selfc = self.canon(it.tyn, m, None)
        have = {}
        for f in fns:
            if f.name in have:
                self.d.error(f.span, f"the method `{f.name}` is defined more than once in this `impl`")
                strays.append(f)
                continue
            have[f.name] = f
            tf = trait.trait_methods.get(f.name)
            if tf is None:
                avail = sorted(trait.trait_methods)
                sg = suggest(f.name, avail)
                self.d.error(f.span, f"`{f.name}` is not a member of the trait `{trait.name}`",
                             label="not in the trait",
                             help=f"did you mean `{sg}`?" if sg else f"declare it in `trait {trait.name}`, or move it to a plain `impl {tname} {{ ... }}` block")
                strays.append(f)
                continue
            if not self.same_sig(tf, f, tm, m, selfc):
                self.d.error(f.span, f"the method `{f.name}` does not match its declaration in the trait `{trait.name}`",
                             label="different signature",
                             notes=[f"the trait declares `{self.sig_text(tf)}` at line {tf.span.line}",
                                    f"this impl has `{self.sig_text(f)}`"],
                             help="make the receiver, the parameter types and the return type agree with the trait")
        missing = [n for n, tf in trait.trait_methods.items() if tf.body is None and n not in have]
        if missing:
            self.d.error(it.span, f"not all required methods of `{trait.name}` are implemented for `{shown}`: "
                         + ", ".join(f"`{n}`" for n in missing),
                         label="missing implementations",
                         notes=[f"`{n}` is declared at line {trait.trait_methods[n].span.line} of the trait" for n in missing],
                         help=f"add `fn {missing[0]}(...) {{ ... }}` to this `impl`")
        out = [f for f in fns if f not in strays]       # the rest is still registered, so one mistake does not cascade
        for n, tf in trait.trait_methods.items():
            if tf.body is not None and n not in have:
                out.append(self.reparse_default(tf, it, trait))
        info = N("TraitImpl", it.span, tparams=list(it.tparams), bounds=getattr(it, "tbounds", None) or {}, node=it,
                 mod=m, trait=trait)
        self.trait_impls[ikey] = info
        return out

    def same_sig(self, tf, f, tm, m, selfc):
        if len(tf.params) != len(f.params):
            return False
        for a, b in zip(tf.params, f.params):
            sa, sb = getattr(a, "self_param", False), getattr(b, "self_param", False)
            if sa != sb or (sa and getattr(a, "self_mut", False) != getattr(b, "self_mut", False)):
                return False
            if not sa and self.canon(a.tyn, tm, selfc) != self.canon(b.tyn, m, selfc):
                return False
        ra = self.canon(tf.ret, tm, selfc) if tf.ret is not None else "void"
        rb = self.canon(f.ret, m, selfc) if f.ret is not None else "void"
        return ra == rb

    def reparse_default(self, tf, it, trait):
        """A default method of a trait, parsed again with `Self` standing for the type of this `impl`."""
        p = Parser.__new__(Parser)
        p.src, p.d, p.toks, p.p = tf.src, self.d, list(tf.toks) + [tf.eof], 0
        p.no_struct, p.undo, p.impl_ty, p.lex_time, p.tbounds = False, None, it.tyn, 0, {}
        node = p.parse_fn(True, extern=False)
        node.default_of = trait.name
        return node

    def resolve_bounds(self, m):
        """Resolve the trait names in every `<T: Trait>` bound and in every supertrait list of module `m`."""
        for s in list(m.syms.values()):
            if s.kind == "trait":
                for tn in s.node.supers:
                    sup = self.resolve_trait(tn, m)
                    if sup is not None and sup not in s.supers:
                        if sup is s:
                            self.d.error(tn.span, f"trait `{s.name}` cannot be its own supertrait")
                        else:
                            s.supers.append(sup)
                continue
            if s.kind not in ("fn", "struct"):
                continue
            tb = getattr(s.node, "tbounds", None)
            if not tb:
                continue
            s.bounds = {}
            for tp, tns in tb.items():
                for tn in tns:
                    tr = self.resolve_trait(tn, m)
                    if tr is not None and tr not in s.bounds.setdefault(tp, []):
                        s.bounds[tp].append(tr)
        for it in m.ast.items:
            if it.kind == "Impl":
                for tp, tns in (getattr(it, "tbounds", None) or {}).items():
                    for tn in tns:
                        self.resolve_trait(tn, m)          # report unknown traits in `impl<T: Missing>` once

    def check_trait_impls(self):
        """Every impl of a trait needs impls of that trait's supertraits; supertrait lists must not form a cycle."""
        for tr in self.traits:
            seen, stack = set(), list(tr.supers)
            while stack:
                x = stack.pop()
                if x is tr:
                    self.d.error(tr.node.span, f"the supertraits of `{tr.name}` form a cycle", label="a trait cannot require itself")
                    tr.supers = []
                    break
                if x.full in seen:
                    continue
                seen.add(x.full)
                stack.extend(x.supers)
        for (tfull, key), info in sorted(self.trait_impls.items(), key=lambda kv: kv[1].span.line):
            for sup in info.trait.supers:
                if (sup.full, key) not in self.trait_impls:
                    tn = info.node.tyn
                    self.d.error(info.span, f"the trait `{info.trait.name}` requires `{sup.name}`, but `{tn.path[-1]}` does not implement it",
                                 label=f"`{tn.path[-1]}: {sup.name}` is required first",
                                 help=f"add `impl {sup.name} for {tn.path[-1]} {{ ... }}`")

    def implements(self, ty, tr):
        """True if `ty` implements the trait `tr`; otherwise a short reason."""
        key = self.type_key(ty)
        info = self.trait_impls.get((tr.full, key)) if key else None
        if info is None:
            return f"the type `{ty}` does not implement `{tr.name}`"
        if info.bounds and ty.kind == "struct" and ty.sym.base is not None:
            for tp, ta in zip(info.tparams, ty.sym.targs):
                for need in self.bound_traits(info, tp):
                    r = self.implements(ta, need)
                    if r is not True:
                        return f"`{ty}` implements `{tr.name}` only when `{tp}: {need.name}`, but {r[0].lower() + r[1:]}"
        return True

    def bound_traits(self, info, tp):
        """The traits named for the type parameter `tp` in the header of an impl (resolved on demand)."""
        out = []
        for tn in info.bounds.get(tp, ()):
            tr = self.resolve_trait(tn, info.mod, quiet=True)
            if tr is not None:
                out.append(tr)
        return out

    def check_bounds(self, sym, targs, span):
        """The type arguments chosen for `sym` must satisfy the `T: Trait` bounds written on its type parameters."""
        bounds = getattr(sym, "bounds", None)
        if not bounds:
            return True
        ok = True
        for tp, ta in zip(sym.tparams, targs):
            for tr in bounds.get(tp, ()):
                why = self.implements(ta, tr)
                if why is True:
                    continue
                ok = False
                what = {"struct": "struct", "fn": "function"}.get(sym.kind, "item")
                self.d.error(span, f"the trait bound `{ta}: {tr.name}` is not satisfied", label=f"`{ta}` does not implement `{tr.name}`",
                             notes=([] if why.startswith("the type") else [why])
                             + [f"required by the bound `{tp}: {tr.name}` on the {what} `{sym.name}`"],
                             help=f"implement it: `impl {tr.name} for {ta} {{ ... }}`" if self.type_key(ta) else
                             f"only structs, enums and built-in types can implement a trait, so `{ta}` cannot satisfy `{tr.name}`")
        return ok

    def declare_drop(self, m, it):
        st = m.syms.get(it.drop_for)
        if st is None or st.kind != "struct":
            self.d.error(it.span, f"cannot find struct `{it.drop_for}` in this module",
                         label="destructors must be declared in the module that defines the type",
                         help=f"write `drop {it.drop_for}(self) {{ ... }}` next to `struct {it.drop_for}`")
            return
        if st.opaque:
            self.d.error(it.span, f"`{st.name}` is an opaque C type and cannot have a destructor",
                         help="give the wrapper struct a destructor instead, e.g. one that holds a `*" + st.name + "`")
            return
        if st.drop_sym is not None:
            self.d.error(it.span, f"`{st.name}` already has a destructor",
                         notes=[f"the first one is at line {st.drop_sym.node.span.line}"])
            return
        n_expect = len(st.tparams)
        ty = it.params[0].tyn.inner
        args = ty.args
        ok = len(it.tparams) == n_expect and len(args) == n_expect and all(
            a.kind == "TName" and len(a.path) == 1 and not a.args and a.path[0] == tp for a, tp in zip(args, it.tparams))
        if not ok:
            want = f"<{', '.join(st.tparams)}>" if st.tparams else ""
            self.d.error(it.span, f"the destructor of `{st.name}` must be generic over exactly the struct's type parameters"
                         if st.tparams else f"`{st.name}` is not generic, so its destructor takes no type parameters",
                         help=f"write `drop{want} {st.name}{want}(self) {{ ... }}`")
            return
        s = Sym("fn", it.name, m, it, False)
        s.tparams = it.tparams
        s.is_drop = True
        if self.define(m, s, it.span):
            st.drop_sym = s
            if not s.tparams:
                self.fns.append(s)

    # ================================================================= types
    def resolve_type(self, tn, m, allow_void=False):
        k = tn.kind
        if k == "TName":
            path = tn.path
            args = getattr(tn, "args", [])
            if len(path) == 1:
                n = path[0]
                if n in self.tenv:
                    if args:
                        self.d.error(tn.span, f"type parameter `{n}` does not take type arguments")
                        return POISON
                    return self.tenv[n]
                if n in PRIMS:
                    if args:
                        self.d.error(tn.span, f"type `{n}` does not take type arguments")
                        return POISON
                    if n == "void" and not allow_void:
                        self.d.error(tn.span, "`void` is not allowed here", help="`void` can only be a return type")
                        return POISON
                    if self.nostd and n == "String":
                        self.need_std(tn.span, "`String`")
                        return POISON
                    return PRIMS[n]
                if n == "error":
                    return ERR
                sym = m.syms.get(n)
                if sym is None and n in self.prelude_types and n not in m.imports:
                    sym = self.prelude_types[n]
                if sym and sym.kind in ("struct", "enum"):
                    return self.use_type_sym(sym, args, tn, m)
                if sym is not None and sym.kind == "trait":
                    self.d.error(tn.span, f"`{n}` is a trait, not a type", label="traits cannot be used as types",
                                 help=f"use it as a bound instead: `fn f<T: {n}>(x: T)`")
                    return POISON
                if n in m.imports:
                    self.d.error(tn.span, f"expected a type, found module `{n}`",
                                 help=f"use `{n}.TypeName`")
                    return POISON
                cands = [s for s, v in m.syms.items() if v.kind in ("struct", "enum")] + list(PRIMS) + list(self.tenv)
                sg = suggest(n, cands)
                self.d.error(tn.span, f"cannot find type `{n}` in this scope",
                             help=f"a type with a similar name exists: `{sg}`" if sg else None)
                return POISON
            if len(path) == 2:
                imp = m.imports.get(path[0])
                if not imp:
                    self.d.error(tn.span, f"cannot find module `{path[0]}`",
                                 help=f"add `import {path[0]};` at the top of the file")
                    return POISON
                imp.used = True
                sym = imp.mod.syms.get(path[1])
                if not sym or sym.kind not in ("struct", "enum"):
                    self.d.error(tn.span, f"module `{path[0]}` has no type `{path[1]}`")
                    return POISON
                if not sym.pub and sym.mod is not m:
                    self.d.error(tn.span, f"type `{path[1]}` is private to module `{path[0]}`",
                                 help="mark it `pub` in its module")
                    return POISON
                return self.use_type_sym(sym, args, tn, m)
            self.d.error(tn.span, "type paths can have at most one module qualifier")
            return POISON
        if k == "TPtr":
            inner = self.resolve_type(tn.inner, m, allow_void=True)
            return POISON if inner.poison else ptr(inner, getattr(tn, "ro", False))
        if k == "TFn":
            ps = [self.resolve_type(p, m) for p in tn.params]
            ret = self.resolve_type(tn.ret, m, allow_void=True) if tn.ret is not None else VOID
            if ret.poison or any(p.poison for p in ps):
                return POISON
            return fnty(ps, ret)
        if k == "TClo":
            ps = [self.resolve_type(p, m) for p in tn.params]
            ret = self.resolve_type(tn.ret, m, allow_void=True) if tn.ret is not None else VOID
            if ret.poison or any(p.poison for p in ps):
                return POISON
            return clo_ty(ps, ret)
        if k == "TSlice":
            inner = self.resolve_type(tn.inner, m)
            return POISON if inner.poison else slc(inner)
        if k == "TArray":
            inner = self.resolve_type(tn.inner, m)
            try:
                n = self.const_eval(tn.size, m)
            except NotConst as e:
                self.d.error(e.span, e.msg)
                return POISON
            if isinstance(n, bool) or not isinstance(n, int):
                self.d.error(tn.size.span, "array length must be an integer constant")
                return POISON
            if n <= 0:
                self.d.error(tn.size.span, "array length must be at least 1")
                return POISON
            if n > 1 << 31:
                self.d.error(tn.size.span, "array length is too large")
                return POISON
            if inner.poison or not self.sized(inner, tn.span):
                return POISON
            return arr(inner, n)
        if k == "TOpt":
            inner = self.resolve_type(tn.inner, m)
            if inner.poison:
                return POISON
            if inner.kind in ("eu", "opt"):
                self.d.error(tn.span, f"`{inner}?` is not allowed: optional and error types cannot be stacked",
                             help="use one of them, or wrap one in a struct")
                return POISON
            return opt(inner)
        if k == "TErrUnion":
            inner = self.resolve_type(tn.inner, m, allow_void=True)
            if inner.poison:
                return POISON
            if inner.kind == "eu":
                self.d.error(tn.span, "error unions cannot be nested")
                return POISON
            return eu(inner)
        raise AssertionError(k)

    def sized(self, t, span):
        """Report an error if `t` is an opaque C type used by value."""
        while t.kind == "arr":
            t = t.inner
        if t.kind == "struct" and t.sym.opaque:
            self.d.error(span, f"`{t.sym.name}` is an opaque C type, so it cannot be used by value",
                         label="its size is unknown", help=f"use a pointer: `*{t.sym.name}`")
            return False
        return True

    def resolve_sigs(self, m):
        for s in list(m.syms.values()):
            it = s.node
            if s.tparams:
                continue
            if s.kind == "fn":
                seen = {}
                for p in it.params:
                    t = self.resolve_type(p.tyn, m)
                    if not t.poison and not self.sized(t, p.tyn.span):
                        t = POISON
                    s.params.append(t)
                    if p.name in seen:
                        self.d.error(p.name_span, f"parameter `{p.name}` is declared more than once")
                    seen[p.name] = 1
                s.ret = self.resolve_type(it.ret, m, allow_void=True) if it.ret else VOID
                if it.ret is not None and not s.ret.poison and not self.sized(s.ret, it.ret.span):
                    s.ret = POISON
            elif s.kind == "struct":
                if not it.fields and not it.opaque:
                    self.d.error(it.span, f"struct `{s.name}` has no fields")
                for f in it.fields:
                    t = self.resolve_type(f.tyn, m)
                    if f.name in s.field_map:
                        self.d.error(f.span, f"field `{f.name}` is declared more than once in `{s.name}`")
                        continue
                    if not t.poison and not self.sized(t, f.tyn.span):
                        t = POISON
                    s.fields.append((f.name, t, f.span))
                    s.field_map[f.name] = t
            elif s.kind == "const":
                t = self.resolve_type(it.tyn, m)
                if s.extern:
                    if not t.poison and not (t.is_numeric or t == BOOL or t.kind == "ptr"):
                        self.d.error(it.tyn.span, f"extern constants must be numbers, `bool` or pointers, not `{t}`",
                                     help="for C strings, bind a function such as `SDL_GetError()` instead")
                        t = POISON
                elif not t.poison and not (t.is_numeric or t == BOOL or t == STR):
                    self.d.error(it.tyn.span, f"constants must be integers, floats, `bool` or `str`, not `{t}`")
                    t = POISON
                s.ty = t

    # ===================================================================== C ABI
    def ffi_ok(self, t, seen=None):
        """Can a value of type `t` cross the C boundary by value?"""
        k = t.kind
        if k == "prim":
            return t.is_numeric or t == BOOL
        if k in ("ptr", "fn", "enum"):
            return True
        if k == "arr":
            return self.ffi_ok(t.inner, seen)
        if k == "struct":
            sym = t.sym
            if sym.opaque:
                return False
            seen = seen if seen is not None else set()
            if sym.full in seen:
                return True
            seen.add(sym.full)
            return all(self.ffi_ok(ft, seen) for _, ft, _ in sym.fields)
        return False

    def ffi_why(self, t):
        if t.kind == "prim" and t.name == "str":
            return "`str` is a Crumbs view (pointer + length); pass `cstr(s)` for a C string"
        if t == STRING:
            return "`String` is a Crumbs type; pass `cstr(s)` or `s.ptr` instead"
        if t.kind == "slice":
            return "slices are (pointer, length) pairs in Crumbs; pass `s.ptr` and `s.len` separately"
        if t.kind == "eu":
            return "error unions have no C equivalent"
        if t.kind == "struct" and t.sym.opaque:
            return f"`{t.sym.name}` is opaque; use `*{t.sym.name}`"
        if t.kind == "struct":
            return f"`{t.sym.name}` contains fields that have no C layout (`str`, slices, ...)"
        if t.kind == "err":
            return "use `i32` and convert errors yourself"
        return None

    def check_ffi(self):
        def check(t, span, what):
            if t.poison or t == VOID:
                return
            if not self.ffi_ok(t):
                self.d.error(span, f"{what} uses type `{t}`, which has no C ABI equivalent", help=self.ffi_why(t))

        for sym in self.fns:
            it = sym.node
            if sym.extern or sym.export:
                what = f"extern function `{sym.name}`" if sym.extern else f"exported function `{sym.name}`"
                for p, t in zip(it.params, sym.params):
                    check(t, p.tyn.span, what)
                if sym.ret.kind == "eu":
                    self.d.error(it.span, f"{what} cannot return an error union", help="return an `i32` status code instead")
                else:
                    check(sym.ret, it.ret.span if it.ret else it.span, what)
        for sym in self.structs:
            if sym.extern_struct:
                for fname, ft, sp in sym.fields:
                    check(ft, sp, f"field `{fname}` of extern struct `{sym.name}`")

    def check_struct_cycles(self):
        state = {}

        def value_structs(t):
            if t.kind == "struct":
                yield t.sym
            elif t.kind == "arr":
                yield from value_structs(t.inner)

        def visit(s, path):
            st = state.get(s.full, 0)
            if st == 2:
                return
            if st == 1:
                chain = " -> ".join(p.name for p in path[path.index(s):] + [s])
                self.d.error(s.node.span, f"recursive type `{s.name}` has infinite size",
                             label="contains itself by value", notes=[f"cycle: {chain}"],
                             help="use a pointer (`*" + s.name + "`) to break the cycle")
                state[s.full] = 2
                return
            state[s.full] = 1
            for _, t, _ in s.fields:
                for d in value_structs(t):
                    visit(d, path + [s])
            state[s.full] = 2

        for s in self.structs:
            visit(s, [])

    # ============================================================= constants
    def const_value(self, sym):
        if sym.evaluated:
            return sym.value
        if sym.extern:
            raise NotConst(sym.node.span, f"`{sym.name}` is defined in C, so its value is not known at compile time")
        if sym.evaluating:
            raise NotConst(sym.node.span, f"constant `{sym.name}` depends on itself")
        sym.evaluating = True
        try:
            sym.value = self.const_eval(sym.node.init, sym.mod)
        finally:
            sym.evaluating = False
        sym.evaluated = True
        return sym.value

    def const_eval(self, e, m):
        k = e.kind
        if k == "Int":
            return e.value
        if k == "Float":
            return float(e.value)
        if k == "Bool":
            return e.value
        if k == "Str":
            return e.value
        if k == "Ident":
            s = m.syms.get(e.name)
            if s and s.kind == "const":
                return self.const_value(s)
            raise NotConst(e.span, f"`{e.name}` is not a constant")
        if k == "Field" and e.obj.kind == "Ident" and e.obj.name in m.imports:
            imp = m.imports[e.obj.name]
            s = imp.mod.syms.get(e.name)
            if s and s.kind == "const" and (s.pub or imp.mod is m):
                imp.used = True
                return self.const_value(s)
            raise NotConst(e.span, f"`{e.obj.name}.{e.name}` is not a public constant")
        if k == "Unary":
            v = self.const_eval(e.expr, m)
            if e.op == "-" and not isinstance(v, (bool, bytes)):
                return -v
            if e.op == "!" and isinstance(v, bool):
                return not v
            if e.op == "~" and isinstance(v, int) and not isinstance(v, bool):
                ty = getattr(e, "ty", None)
                if ty is not None and ty.is_int and not ty.signed:
                    return (~v) & ((1 << ty.bits) - 1)
                return ~v
            raise NotConst(e.span, "operator not allowed in a constant expression")
        if k == "Binary":
            a = self.const_eval(e.l, m)
            b = self.const_eval(e.r, m)
            op = e.op
            try:
                if op in ("&&", "||"):
                    return (a and b) if op == "&&" else (a or b)
                if isinstance(a, bytes) or isinstance(b, bytes):
                    if op == "==":
                        return a == b
                    if op == "!=":
                        return a != b
                    raise NotConst(e.span, "operator not allowed on strings in a constant expression")
                if op == "+":
                    return a + b
                if op == "-":
                    return a - b
                if op == "*":
                    return a * b
                if op == "/":
                    if isinstance(a, float) or isinstance(b, float):
                        return a / b
                    q = abs(a) // abs(b)
                    return q if (a >= 0) == (b >= 0) else -q
                if op == "%":
                    r = abs(a) % abs(b)
                    return r if a >= 0 else -r
                if op == "<<":
                    return a << b
                if op == ">>":
                    return a >> b
                if op == "&":
                    return a & b
                if op == "|":
                    return a | b
                if op == "^":
                    return a ^ b
                if op == "==":
                    return a == b
                if op == "!=":
                    return a != b
                if op == "<":
                    return a < b
                if op == ">":
                    return a > b
                if op == "<=":
                    return a <= b
                if op == ">=":
                    return a >= b
            except ZeroDivisionError:
                raise NotConst(e.span, "division by zero in constant expression")
            raise NotConst(e.span, "operator not allowed in a constant expression")
        if k == "Cast":
            v = self.const_eval(e.expr, m)
            tn = e.tyn
            if tn.kind == "TName" and len(tn.path) == 1 and tn.path[0] in PRIMS:
                t = PRIMS[tn.path[0]]
                if t.is_int and not isinstance(v, bytes):
                    v = int(v)
                    b, s = INTS[t.name]
                    v &= (1 << b) - 1
                    if s and v >= 1 << (b - 1):
                        v -= 1 << b
                    return v
                if t.is_float and not isinstance(v, bytes):
                    return float(v)
            raise NotConst(e.span, "cast not allowed in a constant expression")
        raise NotConst(e.span, "this expression is not a compile-time constant")

    def check_consts(self, m):
        self.m = m
        for s in [x for x in self.consts if x.mod is m]:
            it = s.node
            if s.extern:
                continue
            self.fn = FnInfo(None)
            self.scopes = [{}]
            self.moved = {}
            n0 = self.d.error_count
            t = self.check(it.init, s.ty if not s.ty.poison else None)
            if s.ty.poison or self.d.error_count > n0:
                s.value, s.evaluated = 0, True
                continue
            if not same(t, s.ty):
                self.mismatch(it.init, s.ty, t)
                s.value, s.evaluated = 0, True
                continue
            try:
                v = self.const_value(s)
            except NotConst as e:
                self.d.error(e.span, e.msg, help="constants may only use literals, other constants and operators")
                s.value, s.evaluated = 0, True
                continue
            if s.ty.is_int:
                lo, hi = s.ty.int_range()
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    self.d.error(it.init.span, f"constant `{s.name}` is out of range for `{s.ty}`",
                                 label=f"evaluates to {v}")
                    s.value = 0

    # ============================================================ scopes
    def push(self):
        self.scopes.append({})

    def pop(self):
        sc = self.scopes.pop()
        for loc in sc.values():
            if not loc.used and not loc.is_param and not loc.name.startswith("_"):
                self.d.warn(loc.span, f"unused variable `{loc.name}`",
                            help=f"if this is intentional, rename it to `_{loc.name}`")

    def lookup(self, name):
        crossed = []
        for sc in reversed(self.scopes):
            if name in sc:
                loc = sc[name]
                for info in reversed(crossed):         # outermost closure first
                    loc = info.capture(loc)
                return loc
            if isinstance(sc, ClosureScope):
                crossed.append(sc.clo)
        return None

    def alloc_cname(self, name):
        base = name
        if name in self.c_reserved or name.startswith("cb_") or name.startswith("__"):
            base = name + "_"
        cand, k = base, 0
        while cand in self.fn.names:
            k += 1
            cand = f"{base}__{k}"
        self.fn.names.add(cand)
        return cand

    def declare_local(self, name, ty, mut, span, is_param=False):
        sc = self.scopes[-1]
        if name == "null":
            self.d.error(span, "`null` is a reserved name", help="it is the null pointer literal")
            return Local(name, ty, mut, self.alloc_cname("null_"), span, is_param)   # not entered into scope
        if name in sc and not is_param:
            old = sc[name]
            self.d.error(span, f"variable `{name}` is already declared in this scope",
                         label="redeclared here",
                         notes=[f"first declared at line {old.span.line}"],
                         help="use a different name, or assign to the existing variable (declare it with `let mut`)")
        loc = Local(name, ty, mut, self.alloc_cname(name), span, is_param)
        loc.glue = (not ty.poison) and self.has_glue(ty)
        loc.rdepth = self.fn.rdepth if self.fn is not None else 0
        sc[name] = loc
        return loc

    # ======================================================== function bodies
    def check_bodies(self, m):
        for s in [x for x in self.fns if x.mod is m and x.base is None]:
            self.check_fn_body(s)

    def check_fn_body(self, s):
        it = s.node
        if it.body is None:
            return
        saved = (self.m, self.tenv, self.fn, self.scopes, self.d.context)
        self.m = s.mod
        self.tenv = s.tenv
        if getattr(it, "default_of", None) is not None:
            self.d.context = f"while checking the default method `{it.name}` of trait `{it.default_of}` for this `impl`"
        if s.base is not None:
            binds = ", ".join(f"{k} = {v}" for k, v in s.tenv.items())
            self.d.context = f"while checking `{s.base.name}` instantiated with {binds}"
        self.fn = FnInfo(s)
        self.scopes = [{}]
        self.moved = {}
        s.param_locals = []
        for p, t in zip(it.params, s.params):
            s.param_locals.append(self.declare_local(p.name, t, getattr(p, "mut", False), p.name_span, is_param=True))
            if self.rc and (s.is_drop or (s.base is not None and s.base.is_drop)):
                s.param_locals[-1].glue = False     # --rc: destructors are called on borrowed element addresses, no count held
        n_err = self.d.error_count
        self.check_block(it.body)
        self.pop()
        if self.d.error_count == n_err:           # the borrow checker needs a fully typed body
            self.bck_entries.append(BckEntry(s, it.body, s.param_locals, self.d.context))
        if s.ret.kind != "eu" and s.ret != VOID and not s.ret.poison and not self.terminates(it.body):
            self.d.error(it.span, f"function `{s.name}` does not return a value on every path",
                         label=f"expected `{s.ret}` to be returned",
                         help="add a `return` at the end, or make every branch return")
        self.m, self.tenv, self.fn, self.scopes, self.d.context = saved

    # ============================================================= generics
    def use_type_sym(self, sym, args, tn, m):
        if sym.tparams:
            if sym.kind != "struct":
                return POISON
            if len(args) != len(sym.tparams):
                self.d.error(tn.span, f"struct `{sym.name}` expects {len(sym.tparams)} type argument"
                             f"{'s' if len(sym.tparams) != 1 else ''}, found {len(args)}",
                             help=f"write `{sym.name}<{', '.join(sym.tparams)}>` with concrete types")
                return POISON
            ts = [self.resolve_type(a, m) for a in args]
            if any(t.poison for t in ts):
                return POISON
            return self.inst_struct(sym, ts, tn.span)
        if args:
            self.d.error(tn.span, f"`{sym.name}` is not generic and takes no type arguments")
            return POISON
        return sym.ty

    def too_deep(self, key, span):
        if len(key) > 160 or self.n_inst > 4000:
            self.d.error(span, "generic instantiation recursion limit reached",
                         help="a generic type or function keeps instantiating itself with ever larger types")
            return True
        return False

    def inst_struct(self, sym, targs, span=None):
        key = (sym.full, tuple(t.key for t in targs))
        got = self.inst_cache.get(key)
        if got is not None:
            return got.ty
        if span is not None and self.too_deep("".join(key[1]), span):
            return POISON
        if not self.check_bounds(sym, targs, span or sym.node.span):
            return POISON
        self.n_inst += 1
        inst = Sym("struct", sym.name, sym.mod, sym.node, sym.pub)
        inst.name = f"{sym.name}<{', '.join(str(t) for t in targs)}>"
        inst.full = f"{sym.full}<{','.join(t.key for t in targs)}>"
        inst.cname = f"{sym.cname}__g{self.n_inst}"
        inst.base, inst.targs = sym, list(targs)
        inst.ty = Ty("struct", sym=inst)
        self.inst_cache[key] = inst
        self.structs.append(inst)
        saved = (self.m, self.tenv)
        self.m, self.tenv = sym.mod, dict(zip(sym.tparams, targs))
        for f in sym.node.fields:
            t = self.resolve_type(f.tyn, self.m)
            if f.name in inst.field_map:
                continue
            inst.fields.append((f.name, t, f.span))
            inst.field_map[f.name] = t
        self.m, self.tenv = saved
        if not inst.fields:
            self.d.error(sym.node.span, f"struct `{sym.name}` has no fields")
        if sym.drop_sym is not None:
            inst.drop_inst = self.inst_fn(sym.drop_sym, list(targs), span or sym.node.span)
        return inst.ty

    def clone(self, n):
        if isinstance(n, N):
            c = N.__new__(N)
            for k, v in n.__dict__.items():
                c.__dict__[k] = self.clone(v)
            return c
        if isinstance(n, list):
            return [self.clone(x) for x in n]
        if isinstance(n, tuple):
            return tuple(self.clone(x) for x in n)
        return n

    def inst_fn(self, sym, targs, span):
        key = (sym.full, tuple(t.key for t in targs))
        got = self.inst_cache.get(key)
        if got is not None:
            return got
        if self.too_deep("".join(key[1]), span):
            return None
        if not self.check_bounds(sym, targs, span):
            return None
        self.n_inst += 1
        inst = Sym("fn", sym.name, sym.mod, self.clone(sym.node), sym.pub)
        inst.cname = f"{sym.cname}__g{self.n_inst}"
        inst.base, inst.targs = sym, list(targs)
        inst.tenv = dict(zip(sym.tparams, targs))
        self.inst_cache[key] = inst
        saved = (self.m, self.tenv)
        self.m, self.tenv = sym.mod, inst.tenv
        for p in inst.node.params:
            inst.params.append(self.resolve_type(p.tyn, self.m))
        inst.ret = self.resolve_type(inst.node.ret, self.m, allow_void=True) if inst.node.ret else VOID
        self.m, self.tenv = saved
        self.fns.append(inst)
        self.pending.append(inst)
        return inst

    def unify(self, tn, ty, sym, b):
        """Bind type parameters of `sym` by matching the type AST `tn` against the concrete type `ty`."""
        if ty is None or ty.poison:
            return
        k = tn.kind
        if k == "TName":
            if len(tn.path) == 1 and tn.path[0] in sym.tparams and not tn.args:
                b.setdefault(tn.path[0], ty)
            elif tn.args and ty.kind == "struct" and ty.sym.base is not None and ty.sym.base.name == tn.path[-1]:
                for a, ta in zip(tn.args, ty.sym.targs):
                    self.unify(a, ta, sym, b)
        elif k == "TPtr" and ty.kind == "ptr":
            self.unify(tn.inner, ty.inner, sym, b)
        elif k == "TSlice" and ty.kind == "slice":
            self.unify(tn.inner, ty.inner, sym, b)
        elif k == "TArray" and ty.kind == "arr":
            self.unify(tn.inner, ty.inner, sym, b)
        elif k == "TErrUnion" and ty.kind == "eu":
            self.unify(tn.inner, ty.inner, sym, b)
        elif k in ("TFn", "TClo") and ty.kind == {"TFn": "fn", "TClo": "clo"}[k] and len(tn.params) == len(ty.params):
            for a, p in zip(tn.params, ty.params):
                self.unify(a, p, sym, b)
            if tn.ret is not None:
                self.unify(tn.ret, ty.inner, sym, b)

    def clo_hint(self, tn, sym, b):
        """The closure type a closure argument is checked against: the parameter types known so far (the rest unknown)."""
        saved = (self.m, self.tenv)
        self.m, self.tenv = sym.mod, {**saved[1], **b}

        def known(x):
            if x.kind == "TName" and len(x.path) == 1 and x.path[0] in sym.tparams and x.path[0] not in b and not x.args:
                return False
            return all(known(c) for c in (x.args if x.kind == "TName" else [getattr(x, "inner", None)] if hasattr(x, "inner") else [])
                       if c is not None)
        try:
            ps = [self.resolve_type(x, sym.mod) if known(x) else POISON for x in tn.params]
            if tn.ret is None:
                ret = VOID
            else:
                ret = self.resolve_type(tn.ret, sym.mod, allow_void=True) if known(tn.ret) else POISON
        finally:
            self.m, self.tenv = saved
        return clo_ty(ps, ret)

    def is_bare_param(self, tn, sym, b):
        return tn.kind == "TName" and len(tn.path) == 1 and tn.path[0] in sym.tparams and tn.path[0] in b

    def report_uninferred(self, span, sym, b, what):
        missing = [n for n in sym.tparams if n not in b]
        self.d.error(span, f"cannot infer type parameter{'s' if len(missing) > 1 else ''} "
                     f"{', '.join('`' + m + '`' for m in missing)} of `{sym.name}`",
                     label="type annotations needed",
                     help=f"specify explicitly, e.g. `{sym.name}<{', '.join(sym.tparams)}>(...)`"
                     if what == "fn" else "annotate the variable or give the fields concrete values")

    def probe(self, a):
        """Checks an argument only to learn its type: the moves it makes are undone, since the real check comes later."""
        saved = dict(self.moved)
        t = self.check(a, None)
        self.moved = saved
        return t

    def instantiate_call(self, e, sym, exp):
        tps, params = sym.tparams, sym.node.params
        if len(e.args) != len(params):
            off = 1 if getattr(e, "method_recv", False) else 0
            self.d.error(e.span, f"function `{sym.name}` takes {len(params) - off} argument{'s' if len(params) - off != 1 else ''} "
                         f"but {len(e.args) - off} {'was' if len(e.args) - off == 1 else 'were'} supplied", label="wrong number of arguments")
            for a in e.args:
                self.check(a)
            return None
        b = {}
        if e.targs:
            if len(e.targs) != len(tps):
                self.d.error(e.span, f"`{sym.name}` takes {len(tps)} type argument{'s' if len(tps) != 1 else ''} "
                             f"but {len(e.targs)} {'was' if len(e.targs) == 1 else 'were'} supplied")
                return None
            for n, tn in zip(tps, e.targs):
                t = self.resolve_type(tn, self.m)
                if t.poison:
                    return None
                b[n] = t
        else:
            for a, p in zip(e.args, params):
                if not self.untyped(a) and a.kind != "Closure":
                    self.unify(p.tyn, self.probe(a), sym, b)
            for a, p in zip(e.args, params):
                if self.untyped(a) and a.kind != "Closure" and not self.is_bare_param(p.tyn, sym, b):
                    self.unify(p.tyn, self.probe(a), sym, b)
            for a, p in zip(e.args, params):                    # closures last: their parameter types come from what is already known
                if a.kind == "Closure" and p.tyn.kind == "TClo":
                    self.unify(p.tyn, self.check(a, self.clo_hint(p.tyn, sym, b)), sym, b)
                elif a.kind == "Closure":
                    self.check(a, None)
            r = sym.node.ret
            if exp is not None and not exp.poison and r is not None:
                if r.kind == "TErrUnion":
                    r, exp = r.inner, (exp.inner if exp.kind == "eu" else None)
                if exp is not None:
                    self.unify(r, exp, sym, b)
        if any(n not in b for n in tps):
            if not any(getattr(a, "ty", None) is not None and a.ty.poison for a in e.args):
                self.report_uninferred(e.span, sym, b, "fn")
            for a in e.args:
                self.check(a)
            return None
        return self.inst_fn(sym, [b[n] for n in tps], e.span)

    # ============================================================ destructors
    def drop_fn_of(self, sym):
        if sym.base is not None:
            return sym.drop_inst
        return sym.drop_sym if not sym.tparams else None

    def has_glue(self, t, leaves=True):
        """Does dropping a value of type `t` run any destructor?  (`leaves=False` ignores the `--rc` pointer/slice counts,
        leaving only what must be moved: Strings, closures, destructors.)"""
        k = t.kind
        if t == STRING:
            return True                 # an owned String frees its buffer when it goes out of scope
        if self.rc and leaves and k in ("ptr", "slice"):
            return True                 # --rc: a pointer or slice holds one count on its block
        if k in ("arr", "opt"):
            return self.has_glue(t.inner, leaves)
        if k == "clo":
            return True                 # a closure owns its captured environment
        if k != "struct":
            return False
        key = t.key
        cache = self.glue if leaves else self.glue_nl
        if key in cache:
            return cache[key]
        busy = (key, leaves)
        if busy in self.glue_busy:
            return False
        self.glue_busy.add(busy)
        sym = t.sym
        r = self.drop_fn_of(sym) is not None or any(self.field_glue(sym, ft) if leaves else self.has_glue(ft, False) for _, ft, _ in sym.fields)
        self.glue_busy.discard(busy)
        cache[key] = r
        return r

    def field_glue(self, sym, ft):
        """Does field `ft` of struct `sym` get destroyed with the struct?  A struct with a destructor manages its raw
        pointers and slices itself (under --rc too), so only its real owners (Strings, ...) are dropped for it."""
        if self.rc and self.drop_fn_of(sym) is not None:
            return self.has_glue(ft, False)
        return self.has_glue(ft)

    def rc_copyable(self, t):
        """--rc: a type whose only 'glue' is pointer/slice counts is copied freely (each copy retains)."""
        return self.rc and self.has_glue(t) and not self.has_glue(t, leaves=False)

    def consume(self, e, move=False):
        """`e` is used by value where ownership is taken: a variable is moved, anything else must be a temporary."""
        t = getattr(e, "ty", None)
        if t is None or t.poison or not self.has_glue(t) or (getattr(e, "coerce", False) and t.kind not in ("ptr", "slice")):
            return                      # (a `String` converted to a `str` view is a borrow, not a move)
        k = e.kind
        if not move and self.rc_copyable(t):
            # --rc: copying a pointer/slice (or a struct of them) retains it; fresh values pass their count on
            if k in ("Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch", "Ternary") and \
                    not (k == "Call" and getattr(e, "builtin", None) in ("slice_of", "from_cstr")):
                e.owned = True
            else:
                e.rc_copy = True
            return
        if k == "Ident" and getattr(e, "local", None) is not None:
            if e.local.captured:
                self.d.error(e.span, f"cannot move `{e.name}` out of a closure's captured variables",
                             label="a closure can run more than once", help="clone it, or borrow it with `&`")
                return
            e.move = True
            self.moved[e.local] = e.span
            return
        if k in ("Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch", "Ternary"):
            e.owned = True              # a temporary whose ownership passes to the receiver
            return
        what = None
        if k == "Field" and getattr(e, "mode", None) in ("field", "ptrfield"):
            what = "a struct field"
        elif k == "Index":
            what = "an array or slice element"
        elif k == "Unary" and e.op == "*":
            what = "a pointer"
        if what:
            self.d.error(e.span, f"cannot move a `{t}` out of {what}", label="its type has a destructor, so it cannot be copied",
                         help="borrow it with `&`, or use `read_at(p)` to take ownership of raw storage")

    def merge_moved(self, m0, *states):
        states = [x for x in states if x is not None]
        if not states:
            return dict(m0)
        out = {}
        for st in states:
            for k, v in st.items():
                out.setdefault(k, v)
        return out

    def in_loop(self, body_node, body, cond_exits):
        """Check a loop body (twice if it moves an outer variable) and compute the moved-set after the loop."""
        fn = self.fn
        m0 = dict(self.moved)
        start_id = Local._n
        frame = {"breaks": [], "conts": []}
        fn.loop_stack.append(frame)
        fn.loops += 1
        body()
        carry = ([] if self.terminates(body_node) else [self.moved]) + frame["conts"]
        outer = {l: sp for st in carry for l, sp in st.items() if l not in m0 and l.id <= start_id}
        if outer:
            frame = {"breaks": [], "conts": []}
            fn.loop_stack[-1] = frame
            self.opt_consumed = set()
            self.moved = {**m0, **outer}
            body()
            carry = ([] if self.terminates(body_node) else [self.moved]) + frame["conts"]
        fn.loops -= 1
        fn.loop_stack.pop()
        exits = list(frame["breaks"])
        if cond_exits:
            exits.append(self.merge_moved(m0, *carry) if carry else dict(m0))
        merged = self.merge_moved(m0, *exits) if exits else dict(m0)
        self.moved = {l: sp for l, sp in merged.items() if l.id <= start_id}

    def check_main(self):
        m = self.entry
        nm = self.entry_name
        if m is None or not nm:
            return
        s = m.syms.get(nm)
        if not s or s.kind != "fn":
            from .diag import Span
            self.d.error(Span(m.src, 1, 1, 1), f"module `{m.name}` has no `{nm}` function",
                         help=f"add `fn {nm}() {{ ... }}`, choose another entry point with `--entry=NAME`, "
                              "or use `--entry=` to build without one")
            return
        self.main_sym = s
        if s.tparams:
            self.d.error(s.node.span, f"`{nm}` cannot be generic")
        if s.params:
            self.d.error(s.node.span, f"`{nm}` cannot take parameters")
        ok = s.ret.poison or s.ret == VOID or s.ret == I32 or (s.ret.kind == "eu" and (s.ret.inner == VOID or s.ret.inner == I32))
        if not ok:
            self.d.error(s.node.span, f"`{nm}` must return `void`, `i32`, `void!` or `i32!`, not `{s.ret}`")

    # =============================================================== helpers
    def mismatch(self, e, exp, got, what="mismatched types"):
        help = None
        if exp.is_float and e.kind == "Int" and not e.suffix:
            help = f"write the literal as a float, e.g. `{e.value}.0`"
        elif exp.is_numeric and got.is_numeric:
            help = f"convert explicitly with `as {exp}`"
        elif exp == BOOL and got.is_numeric:
            help = "compare explicitly, e.g. `x != 0`"
        elif got == VOID:
            help = "this expression produces no value"
        self.d.error(e.span, what, label=f"expected `{exp}`, found `{got}`", help=help)

    def ro_violation(self, e, t, exp):
        """Reports (and returns True) when a `*const T` would become a writable `*T`."""
        if t.kind == "ptr" and exp.kind == "ptr" and t.ro and not exp.ro and same(t, exp) and not getattr(e, "recv_ok", False):
            self.d.error(e.span, "cannot turn a read-only pointer into a writable one", label=f"expected `{exp}`, found `{t}`",
                         help=f"take `*const {exp.inner}` instead, or cast with `as` if you really must")
            return True
        return False

    def ptr_ok(self, exp, t):
        """`*T` converts implicitly to `*void` (as in C), never the other way round."""
        return exp.kind == "ptr" and exp.inner == VOID and t.kind == "ptr"

    def expect(self, e, exp, eu_ok=False):
        t = self.check(e, exp, eu=eu_ok)
        return self.coerce(e, t, exp)

    def coerce(self, e, t, exp):
        """Convert the checked expression `e` (of type `t`) implicitly to `exp`, or report a mismatch."""
        e.conv = None
        e.wrap_opt = None
        if exp.kind == "opt" and not same(t, exp):
            if t.poison:
                return POISON
            if self.coerce(e, t, exp.inner).poison:
                return POISON
            e.wrap_opt = exp                                 # a plain value becomes `Some(value)`
            return exp
        if exp == STR and t == STRING:
            e.coerce = True
            return exp
        if exp == STRING and t == STR:                       # a str becomes an owned String (a copy is allocated)
            e.to_string = True
            e.owned = True
            return exp
        if exp.kind == "slice" and t.kind == "arr" and same(t.inner, exp.inner):     # an array lends itself as a slice
            if not self.is_place(e):
                self.d.error(e.span, "cannot turn a temporary array into a slice", label="the array would be gone after the call",
                             help="store it in a variable first")
                return POISON
            e.arr_slice = (t.n, exp)
            return exp
        if exp.kind == "ptr" and t.kind == "slice" and same(t.inner, exp.inner) and not (exp.inner == VOID):   # C-style decay to the first element
            self.consume(e)
            e.slice_decay = True
            return exp
        if exp.is_int and (t.kind == "enum" or t == BOOL):   # enums and bools are numbers underneath
            e.int_cast = exp
            return exp
        if t.is_numeric and exp.is_numeric and t != exp:
            return exp if self.implicit_conv(e, t, exp) else POISON
        if self.ro_violation(e, t, exp):
            return POISON
        if self.ptr_ok(exp, t):
            self.consume(e)
            return exp
        if exp.kind == "ptr" and t.kind == "ptr" and same(t, exp) and exp.ro and not t.ro:
            self.consume(e)
            return exp                                       # `*T` -> `*const T` is always fine
        if not same(t, exp):
            self.mismatch(e, exp, t)
            return POISON
        self.consume(e)
        return t

    def extra_conv(self, t, exp):
        """The implicit conversions beyond numbers, optionals and pointers (see README): str -> String, [N]T -> []T, []T -> *T, enum/bool -> int."""
        return ((exp == STRING and t == STR) or (exp.kind == "slice" and t.kind == "arr") or (exp.kind == "ptr" and t.kind == "slice")
                or (exp.is_int and (t.kind == "enum" or t == BOOL)))

    def untyped(self, e):
        k = e.kind
        if k == "Ident" and e.name == "null" and not self.lookup("null"):
            return True
        if k in ("Int", "Float"):
            return not e.suffix
        if k == "Unary" and e.op == "-":
            return self.untyped(e.expr)
        if k == "Binary" and (e.op in ARITH or e.op in BITWISE):
            return self.untyped(e.l) and self.untyped(e.r)
        return False

    def is_null(self, e):
        return e.kind == "Ident" and e.name == "null" and not self.lookup("null")

    def check(self, e, exp=None, eu=False):
        hint = exp.inner if (exp is not None and exp.kind == "opt" and not self.is_null(e) and e.kind != "Ternary") else exp
        t = getattr(self, "ex_" + e.kind)(e, hint)
        e.ty = t
        if t.kind == "eu" and not eu and self.fn is not None and self.fn.try_stack and e.kind == "Call":
            return self.auto_try(e, t)
        if t.kind == "eu" and not eu:
            self.d.error(e.span, f"unhandled error union `{t}`",
                         label="this call can fail",
                         help="propagate with `try expr`, or handle it with `expr catch default` / `expr catch |e| { ... }`")
            e.ty = POISON
            return POISON
        return t

    def auto_try(self, e, t):
        """Inside `try { }` a failing call needs no `try`: its error goes to the block's handler."""
        e.auto_try = True
        e.eu_ty = t
        self.fn.try_stack[-1]["fails"].append(dict(self.moved))
        e.ty = t.inner
        return t.inner

    def st_TryBlock(self, s):
        fn = self.fn
        frame = {"fails": []}
        fn.try_stack.append(frame)
        m0 = dict(self.moved)
        self.check_block(s.body)
        fn.try_stack.pop()
        m_body = None if self.terminates(s.body) else self.moved
        if not frame["fails"]:
            self.d.warn(s.span, "this `try` block contains nothing that can fail",
                        help="calls to functions returning `T!` and `throw` send control to the `catch` handler")
        self.moved = self.merge_moved(m0, *frame["fails"]) if frame["fails"] else dict(m0)
        self.push()
        s.binder_local = None
        if s.binder:
            loc = self.declare_local(s.binder, ERR, False, s.binder_span)
            loc.used = True
            s.binder_local = loc
        self.check_block(s.handler)
        self.pop()
        m_handler = None if self.terminates(s.handler) else self.moved
        self.moved = self.merge_moved(m0, m_body, m_handler)

    # ============================================================== literals
    def ex_Int(self, e, exp, neg=False):
        v = -e.value if neg else e.value
        if e.suffix:
            ty = PRIMS[e.suffix] if e.suffix in INTS else (F32 if e.suffix == "f32" else F64)
        elif exp is not None and exp.is_int:
            ty = exp
        else:
            ty = I32
        if ty.is_int:
            lo, hi = ty.int_range()
            if not lo <= v <= hi:
                hint = None
                if not e.suffix and (exp is None or not exp.is_int):
                    for cand in (I64, U64):
                        l2, h2 = cand.int_range()
                        if l2 <= v <= h2:
                            hint = f"integers default to `i32`; write `{v}{cand}` or annotate the variable type"
                            break
                self.d.error(e.span, f"integer literal `{v}` is out of range for `{ty}`",
                             label=f"valid range is {lo}..={hi}", help=hint)
        return ty

    def ex_Float(self, e, exp):
        if e.suffix:
            return PRIMS[e.suffix]
        if exp is not None and exp.is_float:
            return exp
        return F64

    def ex_Bool(self, e, exp):
        return BOOL

    def ex_Str(self, e, exp):
        return STR

    # ----------------------------------------------- optionals, ternary
    def ex_Coalesce(self, e, exp):
        """`a ?? b`: `a` if it holds a value (or is a non-null pointer), else `b`."""
        lt = self.check(e.l, None)
        if lt.poison:
            self.check(e.r)
            return POISON
        if lt.kind == "opt":
            T, e.cmode = lt.inner, "opt"
        elif lt.kind == "ptr":
            T, e.cmode = lt, "ptr"
        else:
            self.d.error(e.l.span, f"the left side of `??` must be optional or a pointer, found `{lt}`",
                         label="this can never be empty",
                         help="declare it as `T?`, e.g. `let name: str? = null;`")
            self.check(e.r)
            return POISON
        e.l.conv = None
        rt = self.check(e.r, T)
        if e.cmode == "opt" and rt.kind == "opt" and same(rt.inner, T):
            e.r.conv = None
            e.chain = True
            return rt                                              # `a ?? b` with an optional `b` stays optional
        e.chain = False
        if self.coerce(e.r, rt, T).poison:
            return POISON
        self.consume(e.l)
        return T

    def ex_Ternary(self, e, exp):
        self.expect(e.cond, BOOL)
        r = self.ternary_arms(e, exp)
        if not r.poison:
            self.consume(e.a)               # the chosen arm's value is the result: an owned arm moves into it
            self.consume(e.b)
        return r

    def ternary_arms(self, e, exp):
        if exp is not None and exp.kind == "opt":
            self.expect(e.a, exp)
            self.expect(e.b, exp)
            return exp
        if self.untyped(e.a) and not self.untyped(e.b):
            tb = self.check(e.b, exp)
            if tb.poison:
                self.check(e.a)
                return POISON
            self.expect(e.a, tb)
            e.b.conv = None
            return tb
        ta = self.check(e.a, exp)
        if ta.poison:
            self.check(e.b)
            return POISON
        e.a.conv = None
        if self.expect(e.b, ta).poison:
            return POISON
        return ta

    def ex_OptPayload(self, e, exp):
        return e.ty

    def ex_OptChain(self, e, exp):
        """`a?.b` / `a?.f(x)`: the field or call result, or nothing when `a` is empty (or a null pointer)."""
        ot = self.check(e.obj, None)
        args = e.args or []
        if ot.poison:
            for a in args:
                self.check(a)
            return POISON
        if ot.kind == "opt":
            pt, e.cmode = ot.inner, "opt"
        elif ot.kind == "ptr" and ot.inner != VOID:
            pt, e.cmode = ot, "ptr"
        else:
            self.d.error(e.obj.span, f"`?.` needs an optional value or a pointer, found `{ot}`",
                         label="this is never empty", help="use `.` for values that are always present")
            for a in args:
                self.check(a)
            return POISON
        self.push()
        loc = self.declare_local("$opt", pt, True, e.span)
        loc.used = True
        e.payload_local = loc
        base = N("Ident", e.span, name="$opt")
        node = N("Field", e.span, obj=base, name=e.name, name_span=e.name_span)
        if e.args is not None:
            node = N("Call", e.span, callee=node, args=list(e.args), targ=None, targs=None)
        e.inner = node
        it = self.check(node, None)
        self.pop()
        if it.poison:
            return POISON
        if it == VOID:
            self.d.error(e.span, "`?.` needs a result: this call returns nothing", help="use `if x != null { x.f(); }` instead")
            return POISON
        e.flat = it.kind == "opt"
        return it if e.flat else opt(it)

    OPT_METHODS = ("is_some", "is_none", "unwrap", "unwrap_or")

    def opt_method(self, e):
        c = e.callee
        t = c.obj.ty.inner
        n = c.name
        want = 1 if n == "unwrap_or" else 0
        if len(e.args) != want:
            self.d.error(e.span, f"`{n}` takes {want} argument{'s' if want != 1 else ''} but {len(e.args)} were supplied")
            for a in e.args:
                self.check(a)
            return POISON
        e.opt_method = n
        if n in ("is_some", "is_none"):
            return BOOL
        if n in ("unwrap", "unwrap_or") and self.has_glue(t):
            self.consume(c.obj)             # the payload moves out: the optional is spent
            self.opt_consumed.add(id(c.obj))
        if n == "unwrap_or":
            self.expect(e.args[0], t)
        return t

    # ------------------------------------------------------- Display / interpolation
    def display_impl(self, t):
        """The `to_string` method of `impl Display for T`, or None."""
        tr = self.prelude_traits.get("Display")
        key = self.type_key(t) if t.kind in ("struct", "enum") else None
        if tr is None or key is None or (tr.full, key) not in self.trait_impls:
            return None
        return self.methods.get(key, {}).get("to_string")

    def display_call(self, e, t):
        """`e.to_string()` as a checked call node when the type of `e` implements Display (else None)."""
        if self.display_impl(t) is None:
            return None
        src = getattr(e, "disp_src", e)
        call = N("Call", e.span, callee=N("Field", e.span, obj=src, name="to_string", name_span=e.span), args=[], targ=None, targs=[])
        call.disp_src = src
        ty = self.check(call, None)
        return call if not ty.poison else None

    def display_help(self, t):
        if t.kind == "opt":
            return "unwrap it first (`x ?? default`, `x.unwrap()`), or show it with `{x:?}`"
        if t.kind in ("struct", "enum", "arr", "slice"):
            return "implement `Display` for it (`impl Display for T { fn to_string(self) -> String { .. } }`), or use `{x:?}` for the automatic representation"
        return None

    DEBUG_KINDS = ("struct", "enum", "arr", "slice", "ptr", "fn")

    def debuggable(self, t, seen=None):
        """Can the automatic `{:?}` representation show a value of type `t`? Returns the offending type or None."""
        if t.poison or t.is_numeric or t in (BOOL, STR, STRING, ERR) or t.kind in ("enum", "ptr", "fn"):
            return None
        if t.kind in ("arr", "slice", "opt"):
            return self.debuggable(t.inner)
        if t.kind == "struct":
            seen = seen or set()
            if t.sym.full in seen:
                return None
            seen.add(t.sym.full)
            for _, ft, _ in t.sym.fields:
                bad = self.debuggable(ft, seen)
                if bad is not None:
                    return bad
            return None
        return t

    FMT_BUILTINS = ("print", "println", "format", "panic", "assert")

    def fmt_args(self, e, name):
        """Rust-style format strings: the template literal (first argument; second for `assert`) is turned into an FStr
        node. `{}` takes the next positional argument, `{0}` a numbered one, `{name}` a named argument (`name = value`)
        or else the variable of that name; `{x:spec}` formats. print/println/panic/assert keep working with plain text
        and extra values, as before, when the literal has no braces."""
        from .fmtspec import parse_template, SpecError
        args = e.args
        tidx = 1 if name == "assert" else 0
        if len(args) <= tidx:
            return True
        t = args[tidx]
        if t.kind == "FStr":
            return True                                           # a re-check
        if t.kind != "Str":
            if name == "format":
                self.d.error(t.span, "the first argument of `format` must be a string literal", label="expected a literal",
                             help="the literal is the template: `format(\"{} of {}\", a, b)`")
                return False
            return True
        if name != "format" and b"{" not in t.value and b"}" not in t.value:
            return True
        try:
            items = parse_template(t.value)
        except SpecError as err:
            self.d.error(t.span, err.msg, label="in this format string", help=err.help)
            return False
        extras = args[tidx + 1:]
        pos, named, ok, seen_named = [], {}, True, False
        for a in extras:
            if a.kind == "NamedArg":
                seen_named = True
                if a.name in named:
                    self.d.error(a.name_span, f"the argument `{a.name}` is given twice")
                    ok = False
                named[a.name] = a
            elif seen_named:
                self.d.error(a.span, "a positional argument cannot follow a named argument", help="put the `name = value` arguments last")
                ok = False
            else:
                pos.append(a)
        if not ok:
            return False
        uses = {}                                                 # id(expression) -> times used
        parts, nxt = [], 0
        for it in items:
            if it[0] == "lit":
                parts.append(N("FPart", t.span, lit=it[1], expr=None, spec="", spec_span=None))
                continue
            _, ref, spec = it
            if ref is None:
                ref, nxt = nxt, nxt + 1
            if isinstance(ref, int):
                if ref >= len(pos):
                    self.d.error(t.span, f"the format string uses argument {ref} but only {len(pos)} {'was' if len(pos) == 1 else 'were'} given",
                                 label="in this format string",
                                 help="add the value after the string, or name it: `{total}` with `total = ...`" if not pos else None)
                    return False
                ex = pos[ref]
            elif isinstance(ref, tuple):                          # an expression written inside the braces
                ex = self.parse_fmt_expr(ref[1], t)
                if ex is None:
                    return False
                uses[id(ex)] = 1
            elif ref in named:
                ex = named[ref].expr
            else:                                                 # the variable (or field path) of that name
                segs = ref.split(".")
                ex = N("Ident", t.span, name=segs[0])
                for sname in segs[1:]:
                    ex = N("Field", t.span, obj=ex, name=sname, name_span=t.span)
                uses[id(ex)] = 1
            uses[id(ex)] = uses.get(id(ex), 0) + 1
            part = N("FPart", t.span, lit=None, expr=ex, spec=spec, spec_span=t.span)
            parts.append(part)
        for a in pos:
            if id(a) not in uses:
                self.d.error(a.span, "this argument is never used by the format string", label="unused argument",
                             help="add a `{}` for it")
                ok = False
            elif uses[id(a)] > 1 and a.kind not in ("Ident", "Int", "Float", "Str", "Bool", "Field"):
                self.d.error(a.span, "an argument used more than once must be a simple value",
                             help="bind it to a variable first, then use `{name}`")
                ok = False
        for a in named.values():
            if id(a.expr) not in uses:
                self.d.error(a.name_span, f"the argument `{a.name}` is never used by the format string", label="unused argument",
                             help=f"add `{{{a.name}}}` to the string")
                ok = False
            elif uses[id(a.expr)] > 1 and a.expr.kind not in ("Ident", "Int", "Float", "Str", "Bool", "Field"):
                self.d.error(a.expr.span, "an argument used more than once must be a simple value",
                             help="bind it to a variable first, then use `{name}`")
                ok = False
        if not ok:
            return False
        args[tidx:] = [N("FStr", t.span, parts=parts)]
        return True

    def parse_fmt_expr(self, text, at):
        """Parses the expression written inside `{...}` of a format string; every node gets the string literal's span."""
        from .parser import Parser, N as Node
        from .diag import Source, Diagnostics
        tmp = Diagnostics()
        src = Source(at.span.src.path, text)
        try:
            p = Parser(src, tmp)
            ex = p.parse_expr()
            if p.tok.kind != "eof" and not tmp.has_errors:
                tmp.error(p.tok.span, "unexpected text after the expression")
        except Exception:
            ex = None
            if not tmp.has_errors:
                tmp.error(at.span, "not a valid expression")
        if tmp.has_errors or ex is None:
            self.d.error(at.span, f"`{{{text}}}` is not a valid placeholder", label="in this format string",
                         help=f"the expression did not parse ({tmp.items[0].msg}); placeholders hold nothing, a number, or an expression: `{{}}`, `{{0}}`, `{{a + b}}`")
            return None

        def fix(n):
            if isinstance(n, Node):
                n.span = at.span
                for k, v in list(n.__dict__.items()):
                    if k in ("kind", "span"):
                        continue
                    if k.endswith("span"):
                        setattr(n, k, at.span)
                    else:
                        fix(v)
            elif isinstance(n, (list, tuple)):
                for x in n:
                    fix(x)
        fix(ex)
        return ex

    def ex_FStr(self, e, exp):
        from .fmtspec import parse_spec, check_for, SpecError
        if self.nostd:
            self.need_std(e.span, "string interpolation")
        for part in e.parts:
            if part.expr is None:
                continue
            part.pspec = None
            part.mode = None
            if not hasattr(part, "src"):
                part.src = part.expr
            part.expr = part.src
            spec = None
            if part.spec:
                try:
                    spec = parse_spec(part.spec)
                except SpecError as err:
                    self.d.error(part.spec_span or part.span, err.msg, label="in this format spec", help=err.help)
                    spec = None
            t = self.check(part.expr, None)
            if t.poison:
                continue
            if t.kind == "eu":
                self.d.error(part.expr.span, f"cannot interpolate a value of type `{t}`",
                             label="has a possible error", help="handle the error first with `try` or `catch`")
                continue
            if spec is None:
                spec = parse_spec("")
            part.pspec = spec
            debug = spec.type == "?"
            if debug:
                bad = self.debuggable(t)
                if bad is not None:
                    self.d.error(part.expr.span, f"cannot show a value of type `{bad}` with `:?`",
                                 help="the automatic representation covers numbers, text, enums, pointers, arrays, slices and structs of those")
                    continue
                part.mode = "debug"
                kind = "other"
            else:
                shown = self.display_call(part.expr, t)
                if shown is not None:
                    part.expr, t, part.mode, kind = shown, STRING, "text", "text"
                elif t.is_int:
                    part.mode, kind = "int", "int"
                elif t.is_float:
                    part.mode, kind = "float", "float"
                elif t == BOOL:
                    part.mode, kind = "bool", "bool"
                elif t in (STR, STRING, ERR):
                    part.mode, kind = "text", "text"
                elif t.kind == "enum":
                    part.mode, kind = "enum", "enum"
                elif t.kind in ("ptr", "fn"):
                    part.mode, kind = "ptr", "ptr"
                else:
                    self.d.error(part.expr.span, f"cannot interpolate a value of type `{t}`", label="no text representation",
                                 help=self.display_help(t))
                    continue
            if not debug:
                msg = check_for(spec, kind)
                if msg is not None:
                    self.d.error(part.spec_span or part.span, msg[0], label="in this format spec", help=msg[1])
        return STR

    # ================================================================ names
    def ex_Closure(self, e, exp):
        cached = getattr(e, "clo_type", None)
        if cached is not None:
            return cached
        hint = exp if exp is not None and exp.kind == "clo" and len(exp.params) == len(e.params) else None
        info = ClosureInfo(e.move, e.span)
        ptys = []
        for i, p in enumerate(e.params):
            if p.tyn is not None:
                t = self.resolve_type(p.tyn, self.m)
            elif hint is not None and not hint.params[i].poison:
                t = hint.params[i]
            else:
                self.d.error(p.span, f"cannot infer the type of the closure parameter `{p.name}`",
                             label="type annotations needed", help=f"write `|{p.name}: <type>|`")
                t = POISON
            ptys.append(t)
        if e.ret is not None:
            rt = self.resolve_type(e.ret, self.m, allow_void=True)
        elif hint is not None and not hint.inner.poison:
            rt = hint.inner
        else:
            rt = None
        self.n_inst += 1
        sym = Sym("fn", "closure", self.m, None, False)
        sym.cname = f"{self.m.cprefix}__clo{self.n_inst}"
        sym.params = list(ptys)
        sym.closure = info
        info.sym = sym
        saved = (self.fn, self.scopes, self.moved, self.reinit_ok)
        scope = ClosureScope(info)
        self.scopes = self.scopes + [scope]
        self.moved = {}
        self.reinit_ok = None
        sym.ret = rt if rt is not None else VOID
        self.fn = FnInfo(sym)
        sym.param_locals = [self.declare_local(p.name, t, False, p.name_span, is_param=True) for p, t in zip(e.params, ptys)]
        if e.expr_body:
            if rt is None:
                t = self.check(e.body, None)
                rt = POISON if t.poison else t
                sym.ret = rt
                self.fn.ret = rt
            st = N("ExprStmt", e.body.span, expr=e.body) if rt == VOID else N("Return", e.body.span, expr=e.body)
            body = N("Block", e.body.span, stmts=[st], end_span=e.body.span)
        else:
            body = e.body
        sym.node = N("Fn", e.span, name="closure", params=e.params, ret=e.ret, body=body, pub=False)
        n_err = self.d.error_count
        self.check_block(body)
        self.pop()
        if self.d.error_count == n_err:
            self.bck_entries.append(BckEntry(sym, body, sym.param_locals, self.d.context))
        if sym.ret.kind != "eu" and sym.ret != VOID and not sym.ret.poison and not self.terminates(body):
            self.d.error(e.span, "this closure does not return a value on every path",
                         label=f"expected `{sym.ret}` to be returned", help="add a `return` at the end, or make every branch return")
        self.fn, self.scopes, self.moved, self.reinit_ok = saved
        # the captures, seen from the enclosing function
        for outer, proxy in info.caps.items():
            if outer in self.moved:
                self.d.error(e.span, f"the closure uses `{outer.name}`, which was moved earlier", label="value used after it was moved",
                             notes=[f"`{outer.name}` was moved at line {self.moved[outer].line}"])
            if info.move and outer.glue:
                self.moved[outer] = e.span
        sym.mod = self.m
        sym.export = False
        sym.extern = False
        self.fns.append(sym)
        e.sym = sym
        e.info = info
        e.captures = list(info.caps.items())
        e.local_body = body
        if sym.ret.poison or any(t.poison for t in ptys):
            return POISON
        e.clo_type = clo_ty(ptys, sym.ret)
        return e.clo_type

    def ex_Ident(self, e, exp):
        if e.name == "null" and not self.lookup("null"):
            e.is_null = True
            if exp is not None and exp.kind in ("ptr", "fn", "opt"):
                return exp
            if exp is not None and exp.poison:
                return POISON
            self.d.error(e.span, "cannot infer the type of `null`",
                         help="write `null as *T`, or give the variable a pointer or optional type, e.g. `let p: *i32 = null;` or `let n: i32? = null;`")
            return POISON
        loc = self.lookup(e.name)
        if loc:
            loc.used = True
            e.local = loc
            if loc.region_handle and not getattr(self, "region_ok", False):
                self.d.error(e.span, f"the region `{e.name}` can only be used as the last argument of `alloc_in`",
                             help=f"write `alloc_in(T, n, {e.name})`")
                return POISON
            if loc in self.moved and e is not self.reinit_ok and id(e) not in self.opt_consumed:
                self.d.error(e.span, f"use of moved value `{e.name}`", label="value used after it was moved",
                             notes=[f"`{e.name}` was moved at line {self.moved[loc].line}, and its type has a destructor, so it cannot be copied"],
                             help="move it only once, pass a pointer (`&" + e.name + "`) instead, or re-assign it before using it again")
            return loc.ty
        sym = self.m.syms.get(e.name)
        if sym:
            if sym.kind in ("const", "error"):
                e.sym = sym
                return sym.ty
            if sym.kind == "fn":
                return self.fn_value(e, sym)
            what = {"fn": "function", "struct": "struct", "enum": "enum", "trait": "trait"}[sym.kind]
            h = None
            if sym.kind == "struct":
                h = f"construct it with `{e.name} {{ field: value }}`"
            self.d.error(e.span, f"expected a value, found {what} `{e.name}`", help=h)
            return POISON
        if e.name in self.m.imports:
            self.m.imports[e.name].used = True
            self.d.error(e.span, f"expected a value, found module `{e.name}`", help=f"access its members with `{e.name}.name`")
            return POISON
        cands = [n for sc in self.scopes for n in sc] + [n for n, s in self.m.syms.items() if s.kind in ("const", "error")]
        sg = suggest(e.name, cands)
        self.d.error(e.span, f"cannot find value `{e.name}` in this scope",
                     label="not found", help=f"a similar name exists: `{sg}`" if sg else None)
        return POISON

    def fn_value(self, e, sym):
        """A named function used as a value: a plain code pointer."""
        if sym.tparams:
            self.d.error(e.span, f"generic function `{sym.name}` cannot be used as a value",
                         help="call it directly, or write a small non-generic wrapper function")
            return POISON
        if sym.variadic:
            self.d.error(e.span, f"variadic function `{sym.name}` cannot be used as a value")
            return POISON
        if sym.ret.poison or any(p.poison for p in sym.params):
            return POISON
        e.sym = sym
        e.mode = "sym"
        return fnty(sym.params, sym.ret)

    def entity(self, e):
        """Resolve a Field/Ident chain as module / function / type path. Returns tuple or None."""
        if e.kind == "Ident":
            if self.lookup(e.name):
                return None
            imp = self.m.imports.get(e.name)
            if imp:
                imp.used = True
                return ("mod", imp.mod)
            sym = self.m.syms.get(e.name)
            if sym and sym.kind in ("struct", "enum", "fn"):
                return ("sym", sym)
            if sym is None and e.name in self.prelude_types:
                return ("sym", self.prelude_types[e.name])
            return None
        if e.kind == "Field":
            base = self.entity(e.obj)
            if base and base[0] == "mod":
                mod = base[1]
                sym = mod.syms.get(e.name)
                if sym is None:
                    cands = list(mod.syms)
                    sg = suggest(e.name, cands)
                    self.d.error(e.name_span, f"module `{mod.name}` has no member `{e.name}`",
                                 help=f"did you mean `{sg}`?" if sg else None)
                    return ("bad",)
                if not sym.pub:
                    self.d.error(e.name_span, f"`{e.name}` is private to module `{mod.name}`",
                                 label="private item", help=f"mark it `pub` in `{mod.name}` to export it")
                    return ("bad",)
                return ("sym", sym)
        return None

    def ex_Field(self, e, exp):
        ent = self.entity(e)
        if ent is not None:
            # a module member used as a value (const / error)
            if ent[0] == "bad":
                return POISON
            if ent[0] == "sym":
                sym = ent[1]
                if sym.kind in ("const", "error"):
                    e.mode = "sym"
                    e.sym = sym
                    return sym.ty
                if sym.kind == "fn":
                    return self.fn_value(e, sym)
                self.d.error(e.span, f"expected a value, found {sym.kind} `{sym.name}`")
                return POISON
            self.d.error(e.span, "expected a value, found module")
            return POISON
        ent = self.entity(e.obj)
        if ent is not None:
            if ent[0] == "bad":
                return POISON
            if ent[0] == "sym" and ent[1].kind == "enum":
                sym = ent[1]
                if e.name not in sym.variants:
                    sg = suggest(e.name, sym.variants)
                    self.d.error(e.name_span, f"enum `{sym.name}` has no variant `{e.name}`",
                                 help=f"did you mean `{sg}`?" if sg else None)
                    return POISON
                e.mode = "variant"
                e.sym = sym
                e.vidx = sym.variants.index(e.name)
                return sym.ty
            if ent[0] == "sym":
                self.d.error(e.span, f"`{ent[1].name}` is a {ent[1].kind}, which has no members to access with `.`")
                return POISON
            self.d.error(e.obj.span, "expected a value, found module")
            return POISON
        ot = self.check(e.obj, None)
        if ot.poison:
            return POISON
        while ot.kind == "ptr" and ot.inner.kind == "ptr":          # `**T`: reach through every pointer
            e.obj = N("Unary", e.obj.span, op="*", expr=e.obj)
            ot = self.check(e.obj, None)
        base = ot
        e.mode = "field"
        if ot.kind == "ptr" and (ot.inner.kind in ("struct", "arr", "slice") or ot.inner in (STR, STRING)):
            base = ot.inner
            e.mode = "ptrfield"
        if base.kind in ("arr", "slice") or base in (STR, STRING):
            if e.name == "ptr" and (base.kind == "slice" or base in (STR, STRING)):
                e.mode = "slice_ptr"
                return ptr(base.inner if base.kind == "slice" else U8)
            if e.name == "len":
                e.mode = {"arr": "len_arr", "slice": "len_slice"}.get(base.kind, "len_slice")
                return I64
            if e.name == "cap" and base == STRING:
                e.mode = "cap"
                return I64
            self.d.error(e.name_span, f"type `{base}` has no field `{e.name}`",
                         help="the available fields are `len`, `ptr` and `cap`" if base == STRING else "the available fields are `len` and `ptr`")
            return POISON
        if base.kind == "struct":
            t = base.sym.field_map.get(e.name)
            if t is None:
                sg = suggest(e.name, list(base.sym.field_map))
                self.d.error(e.name_span, f"struct `{base.sym.name}` has no field `{e.name}`",
                             help=f"did you mean `{sg}`?" if sg else f"fields: {', '.join(base.sym.field_map)}")
                return POISON
            return t
        self.d.error(e.name_span, f"type `{ot}` has no field `{e.name}`", label="not a struct")
        return POISON

    # ============================================================== operators
    def ex_Unary(self, e, exp):
        op = e.op
        if op == "-":
            if e.expr.kind == "Int" and not e.expr.suffix and not getattr(e.expr, "parenthesized", False):
                lt = exp if (exp is not None and exp.is_int) else I32
                if not lt.signed:
                    e.expr.ty = lt
                    self.d.error(e.span, f"cannot negate an unsigned integer (`{lt}`)",
                                 help="unsigned values cannot be negative; use a signed type")
                    return POISON
                t = self.ex_Int(e.expr, exp, neg=True)
                e.expr.ty = t
                return t
            t = self.check(e.expr, exp)
            if t.poison:
                return t
            if not (t.is_float or (t.is_int and t.signed)):
                self.d.error(e.span, f"cannot apply unary `-` to type `{t}`",
                             help="negation needs a signed integer or float" if t.is_int else None)
                return POISON
            return t
        if op == "!":
            t = self.check(e.expr, BOOL)
            if t.poison:
                return t
            if t != BOOL:
                self.d.error(e.span, f"cannot apply `!` to type `{t}`", help="`!` works on `bool`; use `~` for bitwise not")
                return POISON
            return BOOL
        if op == "~":
            t = self.check(e.expr, exp)
            if t.poison:
                return t
            if not t.is_int:
                self.d.error(e.span, f"cannot apply `~` to type `{t}`")
                return POISON
            return t
        if op == "*":
            t = self.check(e.expr, None)
            if t.poison:
                return t
            if t.kind != "ptr":
                self.d.error(e.span, f"cannot dereference non-pointer type `{t}`")
                return POISON
            if t.inner == VOID:
                self.d.error(e.span, "cannot dereference a `*void`", help="cast it to a typed pointer first: `p as *T`")
                return POISON
            if not self.sized(t.inner, e.span):
                return POISON
            return t.inner
        if op == "&":
            exp_inner = exp.inner if exp is not None and exp.kind == "ptr" else None
            t = self.check(e.expr, exp_inner)
            if t.poison:
                return t
            if not self.is_place(e.expr) and getattr(e, "auto_ref", False) and e.expr.kind in (
                    "Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch", "Ternary", "Cast", "Field", "Index"):
                e.temp = True                   # `make().method()`: the receiver is a temporary that lives for the statement
                return ptr(t)
            if not self.is_place(e.expr):
                self.d.error(e.span, "cannot take the address of a temporary value",
                             help="store it in a `let mut` variable first")
                return POISON
            if self.through_ro(e.expr):
                return ptr(t, True)                  # a pointer into read-only memory is read-only too
            err = None if getattr(e, "auto_ref", False) else self.place_mut(e.expr)
            if err:
                self.d.error(e.span, "cannot take the address of an immutable binding", label=err[0], help=err[1])
                return POISON
            return ptr(t)
        raise AssertionError(op)

    def is_place(self, e):
        k = e.kind
        if k == "Ident":
            return getattr(e, "local", None) is not None
        if k == "Field":
            return getattr(e, "mode", None) in ("field", "ptrfield")
        if k == "Index":
            return True
        if k == "Unary" and e.op == "*":
            return True
        return False

    def readonly_self(self, e):
        """Is `e` the `self` of the method being checked, and was the method declared with plain `self` (not `mut self`)?"""
        if e.kind != "Ident":
            return False
        loc = getattr(e, "local", None)
        fn = self.fn
        return (loc is not None and loc.is_param and loc.name == "self" and fn is not None and fn.sym is not None
                and getattr(fn.sym, "has_self", False) and not getattr(fn.sym, "self_mut", False))

    def place_mut(self, e):
        """None if `e` is a mutable place; otherwise (label, help)."""
        k = e.kind
        if k == "Ident":
            loc = e.local
            if loc.mut:
                if loc.captured:
                    loc.written = True
                return None
            if loc.is_param:
                return (f"`{loc.name}` is a parameter, and parameters are immutable by default",
                        f"declare it as `mut {loc.name}: {loc.ty}` in the parameter list")
            return (f"`{loc.name}` is declared without `mut`", f"declare it with `let mut {loc.name}`")
        if k == "Field":
            if e.mode == "ptrfield":
                if e.obj.ty.kind == "ptr" and e.obj.ty.ro:
                    return ("this pointer is `*const`, so nothing may be written through it",
                            "take a `*T` (without `const`) if the function must change the value")
                if self.readonly_self(e.obj):
                    return ("this method takes `self`, which only reads the object",
                            "declare the method with `mut self` so it may change its fields (the caller then needs `let mut`)")
                return None
            return self.place_mut(e.obj)
        if k == "Index":
            ot = e.obj.ty
            if ot.kind == "arr" or ot == STRING:
                return self.place_mut(e.obj)
            if ot == STR:
                return ("strings are immutable", "use a `[]u8` slice created with `alloc(u8, n)`")
            return None
        if k == "Unary" and e.op == "*":
            if e.expr.ty.kind == "ptr" and e.expr.ty.ro:
                return ("this pointer is `*const`, so nothing may be written through it",
                        "take a `*T` (without `const`) if the function must change the value")
            return None
        return ("not an assignable place", None)

    def through_ro(self, e):
        """Does the place `e` lie behind a `*const` pointer (so a pointer to it must be `*const` too)?"""
        k = e.kind
        if k == "Field":
            if e.mode == "ptrfield":
                return (e.obj.ty.kind == "ptr" and e.obj.ty.ro) or self.readonly_self(e.obj)
            return self.through_ro(e.obj)
        if k == "Index":
            return self.through_ro(e.obj) if e.obj.ty.kind == "arr" else False
        if k == "Unary" and e.op == "*":
            return e.expr.ty.kind == "ptr" and e.expr.ty.ro
        return False

    def ex_Binary(self, e, exp):
        op = e.op
        l, r = e.l, e.r
        if op in ("&&", "||"):
            e.opty = BOOL
            lt = self.check(l, BOOL)
            rt = self.check(r, BOOL)
            for x, t in ((l, lt), (r, rt)):
                if not t.poison and t != BOOL:
                    self.d.error(x.span, f"`{op}` needs `bool` operands, found `{t}`",
                                 help="compare explicitly, e.g. `x != 0`" if t.is_numeric else None)
                    return BOOL
            return BOOL
        if op == "??":
            return self.ex_Coalesce(e, exp)
        arith_like = op in ARITH or op in BITWISE
        if op in SHIFT:
            lt = self.check(l, exp if exp is not None and exp.is_int else None)
            rt = self.check(r, None)
            e.opty = lt
            if lt.poison or rt.poison:
                return POISON
            if not lt.is_int or not rt.is_int:
                self.d.error(e.op_span, f"cannot apply `{op}` to `{lt}` and `{rt}`", help="shifts need integer operands")
                return POISON
            return lt
        hint = exp if (arith_like and exp is not None and exp.is_numeric) else None
        if self.untyped(l) and not self.untyped(r):
            rt = self.check(r, None)
            lt = self.check(l, rt if (rt.is_numeric or rt.kind in ("ptr", "fn", "opt")) else None)
        else:
            lt = self.check(l, hint)
            rt = self.check(r, lt if (lt.is_numeric or lt.kind in ("ptr", "fn", "opt")) else None)
        e.opty = lt
        if lt.poison or rt.poison:
            return BOOL if op in CMP else POISON
        if op in ("==", "!=") and lt.kind == "opt" and rt.kind == "opt" and (self.is_null(l) or self.is_null(r)):
            if self.is_null(l) and self.is_null(r):
                return BOOL
            e.opt_null = True                                        # `x == null` / `x != null` on an optional
            e.opt_side = "r" if self.is_null(l) else "l"
            for side in (l, r):
                side.conv = None
            return BOOL
        if lt in (STR, STRING) and rt in (STR, STRING) and op in ("+", "==", "!="):
            if op == "+":
                if self.nostd:
                    self.need_std(e.op_span, "joining strings with `+`")
                    return POISON
                res = STRING if STRING in (lt, rt) else STR
                e.strk = "String" if res == STRING else "str"
                e.opty = res
                return res
            e.opty = STR
            return BOOL
        if op in CMP and lt.is_int and rt.is_int:
            zero_r = r.kind == "Int" and r.value == 0 and not lt.signed
            zero_l = l.kind == "Int" and l.value == 0 and not rt.signed
            if (zero_r and op in ("<", ">=")) or (zero_l and op in (">", "<=")):
                always = "false" if (op in ("<", ">") ) else "true"
                self.d.warn(e.op_span, f"this comparison is always {always}", label="unsigned values are never negative",
                            help="remove it, or use a signed type if negative values are possible")
        if lt != rt and lt.is_numeric and rt.is_numeric:
            ct = self.binary_conv(e, lt, rt)
            if ct is None:
                return BOOL if op in CMP else POISON
            lt = rt = ct
            e.opty = ct
        else:
            e.l.conv = None
            e.r.conv = None
        if lt != rt:
            self.d.error(r.span, "mismatched types in binary operation", label=f"expected `{lt}`, found `{rt}`",
                         help=f"convert one side explicitly, e.g. `x as {lt}`" if lt.is_numeric and rt.is_numeric else
                         "convert the other operand with `to_str(x)`" if (lt in (STR, STRING) and op == "+") else
                         f"`{op}` needs both operands to have the same type")
            return BOOL if op in CMP else POISON
        t = lt
        if op in ARITH:
            ok = (t.is_numeric and not (op == "%" and t.is_float)) or (op == "+" and t == STR)
            if not ok:
                self.d.error(e.op_span, f"cannot apply `{op}` to type `{t}`",
                             help="`%` is only defined on integers" if (op == "%" and t.is_float) else None)
                return POISON
            if op in ("/", "%") and t.is_int and r.kind == "Int" and r.value == 0:
                self.d.error(r.span, "division by zero", label="the divisor is the literal 0")
            return t
        if op in BITWISE:
            if not t.is_int:
                self.d.error(e.op_span, f"cannot apply `{op}` to type `{t}`", help="this operator needs integer operands")
                return POISON
            return t
        if op in ("==", "!="):
            if t.is_numeric or t in (BOOL, STR, ERR) or t.kind in ("enum", "ptr", "fn"):
                return BOOL
            self.d.error(e.op_span, f"cannot compare values of type `{t}` with `{op}`",
                         help="compare fields individually" if t.kind in ("struct", "arr", "slice") else None)
            return BOOL
        if op in CMP:
            if t.is_numeric:
                return BOOL
            self.d.error(e.op_span, f"cannot order values of type `{t}` with `{op}`")
            return BOOL
        raise AssertionError(op)

    def ex_Cast(self, e, exp):
        target = self.resolve_type(e.tyn, self.m)
        hint = None
        if target.kind in ("ptr", "fn") and e.expr.kind == "Ident" and e.expr.name == "null":
            hint = target
        elif target.kind == "ptr" and self.untyped(e.expr) and not self.lit_is_float(e.expr):
            hint = U64
        elif self.untyped(e.expr) and target.is_numeric:
            inner_is_float = self.lit_is_float(e.expr)
            if target.is_int and not inner_is_float:
                hint = target
            elif target.is_float and inner_is_float:
                hint = target
        t = self.check(e.expr, hint)
        if target.poison or t.poison:
            return target
        ok = False
        if t == target:
            ok = True
        elif t.is_numeric and target.is_numeric:
            ok = True
        elif t == BOOL and target.is_int:
            ok = True
        elif t.kind == "enum" and target.is_int:
            ok = True
        elif t.kind == "ptr" and (target.is_int or target.kind == "ptr"):
            ok = True
        elif t.is_int and target.kind == "ptr":
            ok = True
        elif t.kind == "fn" and target.kind == "ptr" and target.inner == VOID:
            ok = True
        elif target.kind == "fn" and t.kind == "ptr" and t.inner == VOID:
            ok = True
        if not ok:
            h = None
            if t.is_int and target == BOOL:
                h = "compare explicitly: `x != 0`"
            elif t.is_int and target.kind == "enum":
                h = "enums cannot be created from integers; use `match`"
            elif t.kind == "fn" or target.kind == "fn":
                h = "function pointers convert only to and from `*void`"
            self.d.error(e.span, f"cannot cast `{t}` to `{target}`", help=h)
        return target

    def lit_is_float(self, e):
        if e.kind == "Float":
            return True
        if e.kind == "Unary":
            return self.lit_is_float(e.expr)
        if e.kind == "Binary":
            return self.lit_is_float(e.l) or self.lit_is_float(e.r)
        return False

    def ex_IncDec(self, e, exp):
        t = self.check(e.expr, None)
        if t.poison:
            return POISON
        if not t.is_numeric:
            self.d.error(e.span, f"cannot apply `{e.op}` to type `{t}`", help="`++` and `--` work on integers and floats")
            return POISON
        if not self.is_place(e.expr):
            self.d.error(e.span, f"`{e.op}` needs a variable, field or element to modify", label="not an assignable place")
            return POISON
        err = self.place_mut(e.expr)
        if err:
            self.d.error(e.span, f"cannot apply `{e.op}` to an immutable place", label=err[0], help=err[1])
        return t

    # ======================================================= numeric conversion
    def lint(self, span, msg, label=None, help=None):
        """A numeric-safety diagnostic: a warning, or an error under --strict-casts."""
        if self.strict:
            self.d.error(span, msg, label=label, help=(help + " (error because of --strict-casts)") if help else "error because of --strict-casts")
        else:
            self.d.warn(span, msg, label=label, help=help)

    def common_type(self, a, b):
        """The type both numeric operands are converted to, or None if there is no lossless choice."""
        if a.is_float or b.is_float:
            if a.is_float and b.is_float:
                return F64
            return a if a.is_float else b
        if a.signed == b.signed:
            return a if a.bits >= b.bits else b
        s, u = (a, b) if a.signed else (b, a)
        if s.bits > u.bits:
            return s
        need = u.bits * 2
        return PRIMS[f"i{need}"] if need <= 64 else None

    def conv_kind(self, src, dst):
        if src == dst:
            return None
        if src.is_int and dst.is_int:
            lo_s, hi_s = src.int_range()
            lo_d, hi_d = dst.int_range()
            return "widen" if lo_d <= lo_s and hi_s <= hi_d else "checked"
        if src.is_int and dst.is_float:
            return "widen" if src.bits <= (24 if dst.name == "f32" else 53) else "lossy"
        if src.is_float and dst.is_float:
            return "widen" if src.name == "f32" else "fnarrow"
        return "bad"     # float -> integer is never implicit

    def apply_conv(self, node, src, dst):
        """Record an implicit conversion on `node` (src -> dst). Returns False after reporting an error."""
        node.conv = None
        kind = self.conv_kind(src, dst)
        if kind is None:
            return True
        if kind == "bad":
            self.mismatch(node, dst, src)
            return False
        if kind == "checked":
            try:
                v = self.const_eval(node, self.m)
            except NotConst:
                v = None
            if isinstance(v, int) and not isinstance(v, bool):
                lo, hi = dst.int_range()
                if not lo <= v <= hi:
                    self.d.error(node.span, f"constant value {v} does not fit in `{dst}`",
                                 label=f"valid range is {lo}..={hi}")
                    return False
                kind = "widen"          # constant fits: nothing to check at runtime
            else:
                sign = (f"signed `{src}` to unsigned `{dst}`" if src.signed and not dst.signed else
                        f"unsigned `{src}` to signed `{dst}`" if (not src.signed and dst.signed) else
                        f"`{src}` to narrower `{dst}`" if src.bits > dst.bits else f"`{src}` to `{dst}`")
                self.lint(node.span, f"implicit conversion from {sign} may not fit",
                          label="checked at runtime: panics if the value is out of range",
                          help=f"use `x as {dst}` to truncate on purpose, or widen the destination type")
        elif kind == "lossy":
            self.lint(node.span, f"implicit conversion from `{src}` to `{dst}` may lose precision",
                      label=f"`{dst}` cannot represent every `{src}` exactly",
                      help=f"use `x as {dst}` if the rounding is intended")
        elif kind == "fnarrow":
            self.lint(node.span, "implicit conversion from `f64` to `f32` loses precision",
                      help="use `x as f32` if the rounding is intended")
        node.conv = (src, dst, kind)
        return True

    def implicit_conv(self, e, src, dst):
        return self.apply_conv(e, src, dst)

    def binary_conv(self, e, lt, rt):
        """Convert both operands of a binary operation to a common numeric type. None on error."""
        op = e.op
        ct = self.common_type(lt, rt)
        if ct is None:
            self.d.error(e.op_span, f"cannot mix `{lt}` and `{rt}` in `{op}`",
                         label="no integer type can represent every value of both",
                         help="cast one side explicitly, e.g. `x as i64` (negative values or large values may change)")
            return None
        ok = self.apply_conv(e.l, lt, ct) and self.apply_conv(e.r, rt, ct)
        if not ok:
            return None
        if lt.is_int and rt.is_int and lt.signed != rt.signed:
            sg, us = (lt, rt) if lt.signed else (rt, lt)
            if op in ("<", ">", "<=", ">="):
                self.lint(e.op_span, f"comparing signed `{sg}` with unsigned `{us}`",
                          label=f"both operands are converted to `{ct}`",
                          help="cast one side explicitly (e.g. `x as i32`) to make the intent clear")
            elif op in BITWISE:
                self.lint(e.op_span, f"`{op}` mixes signed `{sg}` and unsigned `{us}` operands",
                          label=f"both operands are converted to `{ct}` (the signed one is sign-extended)",
                          help="cast one side explicitly to make the bit pattern you want unambiguous")
        return ct

    # ===================================================== indexing & slicing
    def check_index_value(self, x):
        t = self.check(x, None)
        if not t.poison and not t.is_int:
            self.d.error(x.span, f"indices must be integers, found `{t}`", help="convert with `as i64`" if t.is_float else None)
            return None
        return t

    def ex_Index(self, e, exp):
        ot = self.check(e.obj, None)
        self.check_index_value(e.idx)
        if ot.poison:
            return POISON
        if ot.kind == "arr":
            if e.idx.kind == "Int" and e.idx.value >= ot.n:
                self.d.error(e.idx.span, "index out of bounds",
                             label=f"the length is {ot.n} but the index is {e.idx.value}")
            return ot.inner
        if ot.kind == "slice":
            return ot.inner
        if ot == STR or ot == STRING:
            return U8
        self.d.error(e.span, f"cannot index into a value of type `{ot}`")
        return POISON

    def ex_SliceExpr(self, e, exp):
        ot = self.check(e.obj, None)
        for x in (e.lo, e.hi):
            if x is not None:
                self.check_index_value(x)
        if ot.poison:
            return POISON
        if ot.kind == "arr":
            if not self.is_place(e.obj):
                self.d.error(e.obj.span, "cannot slice a temporary array", help="store it in a variable first")
                return POISON
            return slc(ot.inner)
        if ot.kind == "slice" or ot == STR:
            return ot
        if ot == STRING:
            return STR
        self.d.error(e.span, f"cannot slice a value of type `{ot}`")
        return POISON

    # ============================================================ aggregates
    def ex_StructLit(self, e, exp):
        ent = self.entity(e.path)
        if ent is None or ent[0] == "bad" or ent[0] != "sym" or ent[1].kind != "struct":
            if ent != ("bad",):
                name = e.path.name if e.path.kind == "Ident" else e.path.name
                self.d.error(e.path.span, f"`{name}` is not a struct", help="struct literals look like `Name { field: value }`")
            for _, _, v in e.fields:
                self.check(v)
            return POISON
        sym = ent[1]
        if sym.opaque:
            self.d.error(e.span, f"`{sym.name}` is an opaque C type and cannot be constructed",
                         help=f"obtain a `*{sym.name}` from a C function")
            for _, _, v in e.fields:
                self.check(v)
            return POISON
        if sym.tparams:
            b = {}
            if exp is not None and exp.kind == "struct" and exp.sym.base is sym:
                b = dict(zip(sym.tparams, exp.sym.targs))
            else:
                fmap = {f.name: f for f in sym.node.fields}
                for nm, sp, v in e.fields:
                    f = fmap.get(nm)
                    if f is not None and not self.untyped(v):
                        self.unify(f.tyn, self.check(v, None), sym, b)
                for nm, sp, v in e.fields:
                    f = fmap.get(nm)
                    if f is not None and self.untyped(v) and not self.is_bare_param(f.tyn, sym, b):
                        self.unify(f.tyn, self.check(v, None), sym, b)
            if any(n not in b for n in sym.tparams):
                self.report_uninferred(e.span, sym, b, "struct")
                for _, _, v in e.fields:
                    self.check(v)
                return POISON
            ty = self.inst_struct(sym, [b[n] for n in sym.tparams], e.span)
            if ty.poison:
                return POISON
            sym = ty.sym
        e.sym = sym
        seen = set()
        for nm, sp, v in e.fields:
            ft = sym.field_map.get(nm)
            if ft is None:
                sg = suggest(nm, list(sym.field_map))
                self.d.error(sp, f"struct `{sym.name}` has no field `{nm}`",
                             help=f"did you mean `{sg}`?" if sg else f"fields: {', '.join(sym.field_map)}")
                self.check(v)
                continue
            if nm in seen:
                self.d.error(sp, f"field `{nm}` is specified more than once")
            seen.add(nm)
            self.expect(v, ft)
        missing = [n for n in sym.field_map if n not in seen]
        if missing:
            self.d.error(e.span, f"missing field{'s' if len(missing) > 1 else ''} in initializer of `{sym.name}`",
                         label="missing " + ", ".join(f"`{m}`" for m in missing))
        return sym.ty

    def ex_ArrayLit(self, e, exp):
        elem_exp = exp.inner if exp is not None and exp.kind == "arr" else None
        if not e.elems:
            if exp is not None and exp.poison:
                return POISON
            if elem_exp is None:
                self.d.error(e.span, "cannot infer the type of an empty array", help="annotate the variable, e.g. `let a: [3]i32 = ...`")
                return POISON
            return exp
        first = self.check(e.elems[0], elem_exp)
        tys = [first]
        for x in e.elems[1:]:
            t = self.check(x, first if not first.poison else elem_exp)
            tys.append(t)
            if not same(t, first) and not (elem_exp is None and t.is_numeric and first.is_numeric):
                self.mismatch(x, first, t, "array elements must all have the same type")
        if elem_exp is None and not first.poison and all(t.is_numeric for t in tys) and any(t != first for t in tys):
            ct = tys[0]                    # mixed numbers: every element converts to the type that holds them all
            for t in tys[1:]:
                ct = self.common_type(ct, t) if ct is not None else None
            if ct is None:
                self.d.error(e.span, "array elements have numeric types with no common type", help="convert some elements with `as`")
                return POISON
            for x, t in zip(e.elems, tys):
                if t != ct and not self.apply_conv(x, t, ct):
                    return POISON
            first = ct
        for x in e.elems:
            self.consume(x)
        return POISON if first.poison else arr(first, len(e.elems))

    def ex_ArrayRepeat(self, e, exp):
        elem_exp = exp.inner if exp is not None and exp.kind == "arr" else None
        t = self.check(e.elem, elem_exp)
        try:
            n = self.const_eval(e.count, self.m)
        except NotConst as ex:
            self.d.error(ex.span, ex.msg, help="the repeat count must be a compile-time constant")
            return POISON
        if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
            self.d.error(e.count.span, "array length must be a positive integer constant")
            return POISON
        e.n = n
        if not t.poison and self.rc_copyable(t):
            self.consume(e.elem)         # --rc: the copies share one block; each holds a count
        elif not t.poison and self.has_glue(t):
            self.d.error(e.span, f"cannot repeat a value of type `{t}`: it has a destructor and cannot be copied",
                         help="write the elements out one by one, e.g. `[make(), make()]`")
            return POISON
        return POISON if t.poison else arr(t, n)

    # =================================================================== calls
    def callee_is_value(self, c, ent):
        """Is the callee an ordinary expression (local, field, element, call result) rather than a named function?"""
        if c.kind not in ("Ident", "Field"):
            return True
        if ent is not None:
            return False
        root = c
        while root.kind == "Field":
            root = root.obj
        if root.kind != "Ident":
            return True
        return self.lookup(root.name) is not None

    LITERAL_DEFAULTS = ("Int", "Float", "Bool", "Str")

    def default_ok(self, d):
        if d.kind in self.LITERAL_DEFAULTS or (d.kind == "Ident" and d.name == "null"):
            return True
        return d.kind == "Unary" and d.op == "-" and d.expr.kind in ("Int", "Float")

    def normalize_args(self, e, sym):
        """Match the written arguments to the parameters: positional first, then `name = value`, then defaults.
        Leaves `e.args` as a plain positional list (the receiver, if any, first). False after an error."""
        params = getattr(sym.node, "params", None)
        if params is None:
            return True
        has_named = any(a.kind == "NamedArg" for a in e.args)
        n_pos = sum(1 for a in e.args if a.kind != "NamedArg")
        if not has_named and (n_pos >= len(params) or not any(getattr(p, "default", None) is not None for p in params)):
            return True                                         # the common case: nothing to do
        off = 1 if getattr(e, "method_recv", False) else 0
        fname = f"function `{sym.name}`"
        slots = [None] * len(params)
        ok, seen_named, extra = True, False, []
        for a in e.args:
            if a.kind == "NamedArg":
                seen_named = True
                names = [p.name for p in params]
                if a.name not in names or names.index(a.name) < off or getattr(params[names.index(a.name)], "self_param", False):
                    sg = suggest(a.name, [p.name for p in params[off:]])
                    self.d.error(a.name_span, f"{fname} has no parameter named `{a.name}`",
                                 label="no such parameter", help=f"did you mean `{sg}`?" if sg else
                                 "parameters: " + ", ".join(p.name for p in params[off:]) if params[off:] else None)
                    self.check(a.expr)
                    ok = False
                    continue
                i = names.index(a.name)
                if slots[i] is not None:
                    self.d.error(a.name_span, f"the argument `{a.name}` is given twice", label="already supplied")
                    self.check(a.expr)
                    ok = False
                    continue
                slots[i] = a.expr
            else:
                if seen_named:
                    self.d.error(a.span, "a positional argument cannot follow a named argument",
                                 help="put the `name = value` arguments last")
                    self.check(a)
                    ok = False
                    continue
                pos = sum(1 for x in e.args[:e.args.index(a)] if x.kind != "NamedArg")
                if pos < len(params):
                    slots[pos] = a
                else:
                    extra.append(a)
        if not ok:
            for v in slots + extra:
                if v is not None and not hasattr(v, "ty"):
                    self.check(v)
            return False
        missing = []
        for i, p in enumerate(params):
            if slots[i] is None:
                d = getattr(p, "default", None)
                if d is None:
                    missing.append(p.name)
                    continue
                if not self.default_ok(d):
                    if not getattr(d, "_reported", False):
                        d._reported = True
                        self.d.error(d.span, "a default value must be a literal", label="not a literal",
                                     help="use a number, string, `true` / `false` or `null`")
                    return False
                slots[i] = self.clone(d)
        if missing:
            plural = "s" if len(missing) > 1 else ""
            self.d.error(e.span, f"{fname} is missing the argument{plural} {', '.join(f'`{m}`' for m in missing)}",
                         label="missing argument" + plural,
                         notes=["signature: fn " + sym.name + "(" + ", ".join(
                             p.name + ("=.." if getattr(p, "default", None) is not None else "") for p in params[off:]) + ")"])
            for v in slots + extra:
                if v is not None:
                    self.check(v)
            return False
        e.args = slots + extra
        return True

    def ex_NamedArg(self, e, exp):
        self.d.error(e.span, "named arguments only work when calling a function that is declared with `fn`",
                     label="not allowed here", help="pass the value positionally")
        self.check(e.expr)
        return POISON

    def check_args(self, e, params, name, sig=None, variadic=False):
        n = len(params)
        bad = len(e.args) < n or (len(e.args) > n and not variadic)
        if bad:
            off = 1 if getattr(e, "method_recv", False) else 0       # the receiver is not written in the call
            plural = "s" if n - off != 1 else ""
            self.d.error(e.span, f"{name} takes {'at least ' if variadic else ''}{n - off} argument{plural} "
                         f"but {len(e.args) - off} {'was' if len(e.args) - off == 1 else 'were'} supplied",
                         label="wrong number of arguments", notes=[f"signature: {sig}"] if sig else None)
        for i, a in enumerate(e.args):
            if i < n:
                self.expect(a, params[i])
            elif variadic and not bad:
                self.vararg(a)
            else:
                self.check(a)

    def vararg(self, a):
        t = self.check(a, None)
        a.conv = None
        if t.poison or t.is_numeric or t == BOOL or t.kind in ("ptr", "enum", "fn"):
            return
        self.d.error(a.span, f"cannot pass a value of type `{t}` to a C variadic parameter",
                     help="pass `cstr(s)` for a C string, or an integer, float, bool or pointer")

    def indirect_call(self, e):
        c = e.callee
        t = self.check(c, None)
        if t.poison:
            for a in e.args:
                self.check(a)
            return POISON
        if t.kind not in ("fn", "clo"):
            self.d.error(c.span, f"cannot call a value of type `{t}`", label="not a function",
                         help="only functions, function pointers (`fn(A) -> R`) and closures (`|A| -> R`) can be called")
            for a in e.args:
                self.check(a)
            return POISON
        e.indirect = "clo" if t.kind == "clo" else True
        self.check_args(e, t.params, "this function pointer", sig=str(t))
        return t.inner

    def ex_Call(self, e, exp):
        c = e.callee
        if c.kind == "Ident" and c.name in BUILTINS and not self.lookup(c.name):
            return self.builtin(e, c.name)
        ent = self.entity(c) if c.kind in ("Ident", "Field") else None
        if ent == ("bad",):
            for a in e.args:
                self.check(a)
            return POISON
        if c.kind == "Field" and (ent is None or (ent[0] == "sym" and ent[1].kind != "fn")):
            if not hasattr(e, "orig_args"):
                e.orig_args = list(e.args)
            e.args = list(e.orig_args)
            e.method_recv = False
            hit = self.find_method(e)
            if hit == "bad":
                return POISON
            if hit == "opt":
                return self.opt_method(e)
            if hit is not None:
                ent = ("sym", hit)
        if ent is None or ent[0] != "sym" or ent[1].kind != "fn":
            if self.callee_is_value(c, ent):
                return self.indirect_call(e)
            if c.kind == "Ident" and not self.lookup(c.name) and c.name not in self.m.syms and c.name not in self.m.imports:
                cands = [n for n, s in self.m.syms.items() if s.kind == "fn"] + sorted(BUILTINS)
                sg = suggest(c.name, cands)
                self.d.error(c.span, f"cannot find function `{c.name}` in this scope", label="not found",
                             help=f"a similar function exists: `{sg}`" if sg else None)
            elif c.kind == "Field" and c.obj.kind == "Ident" and ent is None and not self.lookup(c.obj.name) \
                    and c.obj.name not in self.m.syms and c.obj.name not in self.m.imports:
                self.d.error(c.obj.span, f"cannot find module or value `{c.obj.name}` in this scope",
                             help=f"add `import {c.obj.name};` if it is a module")
            elif ent is not None and ent[0] == "sym" and ent[1].kind == "struct":
                self.d.error(c.span, f"`{ent[1].name}` is a struct, not a function",
                             help=f"construct it with `{ent[1].name} {{ field: value }}`")
            else:
                self.d.error(c.span, "this expression is not callable", help="only functions and function pointers can be called")
            for a in e.args:
                self.check(a)
            return POISON
        sym = ent[1]
        if not self.normalize_args(e, sym):
            return POISON
        if sym.tparams:
            sym = self.instantiate_call(e, sym, exp)
            if sym is None:
                return POISON
        elif e.targs:
            self.d.error(e.span, f"function `{sym.name}` is not generic and takes no type arguments")
        e.sym = sym
        sig = f"fn {sym.name}({', '.join(f'{p.name}: {t}' for p, t in zip(sym.node.params, sym.params))}{', ...' if sym.variadic else ''})" \
              + (f" -> {sym.ret}" if sym.ret != VOID else "")
        self.check_args(e, sym.params, f"function `{sym.name}`", sig=sig, variadic=sym.variadic)
        if self.rc and sym.extern:
            for a in e.args:
                a.rc_copy = False           # C code does not release what it is given: pass a borrow
        return sym.ret

    def find_method(self, e):
        """`Type.f(args)` / `value.f(args)`: look the function up among the methods of the type.
        Returns the method Sym (with the receiver already prepended to e.args), "bad", or None."""
        c = e.callee
        obj = c.obj
        sym = None
        tent = self.entity(obj) if obj.kind in ("Ident", "Field") else None
        if tent == ("bad",):
            return "bad"
        key = None
        recv_t = None
        if tent is not None:
            if tent[0] == "sym" and tent[1].kind in ("struct", "enum"):
                key = tent[1].full
            else:
                return None
        elif obj.kind == "Ident" and obj.name in PRIMS and not self.lookup(obj.name) and obj.name != "void":
            key = obj.name
        if key is not None:
            meth = self.methods.get(key, {}).get(c.name)
            if meth is None:
                tn = obj.name if obj.kind == "Ident" else obj.name
                avail = sorted(self.methods.get(key, {}))
                sg = suggest(c.name, avail) if avail else None
                self.d.error(c.name_span, f"type `{tn}` has no function `{c.name}`",
                             help=f"did you mean `{sg}`?" if sg else None)
                for a in e.args:
                    self.check(a)
                return "bad"
            if not self.method_visible(meth, c):
                return "bad"
            return meth
        if obj.kind == "Ident" and not self.lookup(obj.name):
            return None                             # unknown name: the caller reports it
        # an expression: a struct field holding a function pointer wins over a method of the same name
        ot = self.check(obj, None)
        if ot.poison:
            for a in e.args:
                self.check(a)
            return "bad"
        while ot.kind == "ptr" and ot.inner.kind == "ptr":          # `**T`: reach through every pointer
            obj = c.obj = N("Unary", obj.span, op="*", expr=obj)
            ot = self.check(obj, None)
        if ot.kind == "opt" and c.name in self.OPT_METHODS:
            return "opt"
        base = ot.inner if (ot.kind == "ptr" and ot.inner != VOID) else ot
        if base.kind == "struct" and c.name in base.sym.field_map:
            return None
        chain = [base]
        if base == STRING:
            chain.append(STR)                       # a String can use every `str` function (as a borrowed view)
        for cand in chain:
            k = self.type_key(cand)
            meth = self.methods.get(k, {}).get(c.name) if k else None
            if meth is None or not meth.has_self:
                continue
            if not self.method_visible(meth, c):
                return "bad"
            arg = self.receiver_arg(obj, ot, cand, meth, base)
            if arg is None:
                for a in e.args:
                    self.check(a)
                return "bad"
            e.args = [arg] + e.args
            e.method_recv = True
            return meth
        avail = sorted({n for cand in chain for n, mm in self.methods.get(self.type_key(cand), {}).items() if mm.has_self}) \
            if self.type_key(base) else []
        sg = suggest(c.name, avail) if avail else None
        if base.kind in ("struct", "enum", "prim") or ot.kind == "ptr":
            self.d.error(c.name_span, f"type `{base}` has no method `{c.name}`",
                         help=f"did you mean `{sg}`?" if sg else None)
            for a in e.args:
                self.check(a)
            return "bad"
        return None

    def method_visible(self, meth, c):
        if meth.pub or meth.mod is self.m:
            return True
        self.d.error(c.name_span, f"method `{meth.method_name}` is private to module `{meth.mod.name}`",
                     label="private item", help="mark it `pub` in its `impl` block")
        return False

    def receiver_arg(self, obj, ot, cand, meth, base):
        """Adapt the receiver expression to the method's first parameter (auto-borrow / auto-deref)."""
        p0 = meth.node.params[0]
        by_ptr = p0.tyn.kind == "TPtr"
        if by_ptr:
            if ot.kind == "ptr":
                obj.recv_ok = True                                      # (a `mut self` call gets its own error below)
                if meth.self_mut and ot.ro:
                    self.d.error(obj.span, f"cannot call `{meth.method_name}`, which takes `mut self`, through a `*const` pointer",
                                 label="this pointer is read-only", help="take a `*T` (without `const`) instead")
                if meth.self_mut and self.readonly_self(obj):
                    self.d.error(obj.span, f"cannot call `{meth.method_name}`, which takes `mut self`, on the read-only `self`",
                                 label="this method was declared with plain `self`",
                                 help="declare the calling method with `mut self` too")
                return obj                                          # already a pointer
            if cand != base:                                        # String -> str needs a by-value view, not a pointer
                return obj
            if not self.is_place(obj) and getattr(obj, "kind", "") != "Ident":
                pass
            node = N("Unary", obj.span, op="&", expr=obj)
            node.auto_ref = not meth.self_mut                       # `self` may borrow immutable bindings; `mut self` may not
            node.recv_ok = not meth.self_mut
            return node
        if ot.kind == "ptr":
            return N("Unary", obj.span, op="*", expr=obj)
        return obj

    PRINTABLE_KINDS = ("enum", "ptr", "fn")

    NOSTD_BANNED = {"print", "println", "alloc", "free", "exit", "to_str", "string_new", "string_from",
                    "string_clear", "cstr", "format", "gc_collect", "gc_bytes", "gc_collections", "alloc_in",
                    "gc_collect_gen", "gc_gen_bytes", "gc_gen_collections"}

    def need_std(self, span, what):
        self.d.error(span, f"{what} needs the standard runtime, which `--no-stdruntime` removes",
                     label="not available in this build",
                     help="drop --no-stdruntime, or call the C library yourself through `extern fn`")

    def builtin(self, e, name):
        e.builtin = name
        args = e.args
        if self.nostd and name in self.NOSTD_BANNED:
            self.need_std(e.span, f"`{name}`")
            for a in args:
                self.check(a)
            return POISON
        if name == "gc_collect_gen" and self.gc_mode != "manual":
            why = "the collector is off (`--no-gc`)" if self.gc_mode == "off" else f"this build uses `--gc-mode={self.gc_mode}`"
            self.d.error(e.span, "`gc_collect_gen` needs the manual collector mode",
                         label="not available in this build", notes=[why],
                         help="build with `--gc-mode=manual` (cookie: `gc_mode = \"manual\"`) to decide yourself when each generation is collected; "
                              "`gc_collect()` works in every mode")
            for a in args:
                self.check(a)
            return POISON

        if name in self.FMT_BUILTINS:
            if not self.fmt_args(e, name):
                for a in e.args:
                    self.check(a.expr if a.kind == "NamedArg" else a)
                return POISON
            args = e.args
        if name == "format":
            if not args:
                self.d.error(e.span, "`format` takes a format string: `format(\"{} items\", n)`")
                return POISON
            self.check(args[0], None)
            return STRING

        def arity(n_min, n_max=None):
            n_max = n_min if n_max is None else n_max
            if not n_min <= len(args) <= n_max:
                want = f"{n_min}" if n_min == n_max else f"{n_min} to {n_max}"
                self.d.error(e.span, f"`{name}` takes {want} argument(s) but {len(args)} were supplied")
                for a in args:
                    self.check(a)
                return False
            return True

        if name in ("print", "println"):
            for i, a in enumerate(args):
                t = self.check(a, None)
                if t.poison:
                    continue
                shown = self.display_call(a, t)
                if shown is not None:                       # a user `impl Display`: print what `to_string` returns
                    args[i] = shown
                    continue
                if not (t.is_numeric or t in (BOOL, STR, STRING, ERR) or t.kind in self.PRINTABLE_KINDS):
                    self.d.error(a.span, f"cannot print a value of type `{t}`",
                                 help=self.display_help(t))
            return VOID
        if name == "string_new":
            arity(0)
            return STRING
        if name == "string_from":
            if arity(1):
                self.expect(args[0], STR)
            return STRING
        if name == "string_clear":
            if arity(1):
                t = self.check(args[0], None)
                if not t.poison and t != ptr(STRING):
                    self.d.error(args[0].span, f"`string_clear` expects `*String`, found `{t}`", help="pass `&s` (declared `let mut`)")
            return VOID
        if name == "to_str":
            if arity(1):
                t = self.check(args[0], None)
                shown = None if t.poison else self.display_call(args[0], t)
                if shown is not None:
                    args[0] = shown
                    shown.owned = True      # `to_str` returns a `str`: the `String` behind it is never freed (like the numbers' text)
                elif not t.poison and not (t.is_numeric or t in (BOOL, STR, STRING, ERR) or t.kind == "enum"):
                    self.d.error(args[0].span, f"cannot convert a value of type `{t}` to `str`", help=self.display_help(t))
            return STR
        if name == "panic":
            if arity(1):
                self.expect(args[0], STR)
            return VOID
        if name == "assert":
            if arity(1, 2):
                self.expect(args[0], BOOL)
                if len(args) == 2:
                    self.expect(args[1], STR)
            return VOID
        if name == "exit":
            if arity(1):
                self.expect(args[0], I32)
            return VOID
        if name == "alloc":
            t = self.resolve_type(e.targ, self.m)
            if len(args) != 1:
                self.d.error(e.span, "`alloc` takes a type and a count: `alloc(T, n)`")
                for a in args:
                    self.check(a)
                return POISON
            nt = self.check(args[0], I64 if self.untyped(args[0]) else None)
            if not nt.poison and not nt.is_int:
                self.d.error(args[0].span, f"allocation count must be an integer, found `{nt}`")
            e.elem = t
            return POISON if t.poison else slc(t)
        if name == "alloc_in":
            t = self.resolve_type(e.targ, self.m)
            if len(args) != 2:
                self.d.error(e.span, "`alloc_in` takes a type, a count and a region: `alloc_in(T, n, r)`")
                for a in args:
                    if not (a.kind == "Ident" and self.lookup(a.name) is not None and self.lookup(a.name).region_handle):
                        self.check(a, None)
                return POISON
            nt = self.check(args[0], I64 if self.untyped(args[0]) else None)
            if not nt.poison and not nt.is_int:
                self.d.error(args[0].span, f"allocation count must be an integer, found `{nt}`")
            r = args[1]
            loc = self.lookup(r.name) if r.kind == "Ident" else None
            if loc is None or not loc.region_handle:
                n0 = self.d.error_count
                self.check(r, None)
                if self.d.error_count == n0:
                    self.d.error(r.span, "the last argument of `alloc_in` must be a region",
                                 help="declare one with `region r { ... }` and allocate inside the block")
                return POISON
            loc.used = True
            r.local = loc
            e.region_local = loc
            e.elem = t
            return POISON if t.poison else slc(t)
        if name == "gc_collections":
            arity(0)
            return I64
        if name == "gc_collect":
            arity(0)
            return VOID
        if name == "gc_bytes":
            arity(0)
            return I64
        if name in ("gc_collect_gen", "gc_gen_bytes", "gc_gen_collections"):
            if arity(1):
                self.expect(args[0], I64)
            return VOID if name == "gc_collect_gen" else I64
        if name == "cstr":
            if arity(1):
                t = self.check(args[0], None)
                if not t.poison and t not in (STR, STRING):
                    self.d.error(args[0].span, f"`cstr` expects a `str` or `String`, found `{t}`")
            return ptr(U8)
        if name == "from_cstr":
            if arity(1):
                t = self.check(args[0], None)
                if not t.poison and not (t.kind == "ptr" and t.inner in (U8, I8)):
                    self.d.error(args[0].span, f"`from_cstr` expects a `*u8` (a NUL-terminated C string), found `{t}`",
                                 help="cast other pointers first: `p as *u8`")
            return STR
        if name == "slice_of":
            if arity(2):
                pt = self.check(args[0], None)
                nt = self.check(args[1], I64 if self.untyped(args[1]) else None)
                if not nt.poison and not nt.is_int:
                    self.d.error(args[1].span, f"a slice length must be an integer, found `{nt}`")
                if pt.poison:
                    return POISON
                if pt.kind != "ptr" or pt.inner == VOID:
                    self.d.error(args[0].span, f"`slice_of` expects a typed pointer, found `{pt}`",
                                 help="cast a `*void` first: `p as *T`")
                    return POISON
                return slc(pt.inner)
            return POISON
        if name == "size_of":
            t = self.resolve_type(e.targ, self.m)
            if not t.poison and not self.sized(t, e.targ.span):
                return I64
            e.elem = t
            return I64
        if name == "drop":
            if arity(1):
                self.check(args[0], None)
                self.consume(args[0], move=True)
            return VOID
        if name == "drop_at":
            if arity(1):
                t = self.check(args[0], None)
                if not t.poison and (t.kind != "ptr" or t.inner == VOID):
                    self.d.error(args[0].span, f"`drop_at` expects a typed pointer, found `{t}`")
            return VOID
        if name == "write_at":
            if arity(2):
                pt = self.check(args[0], None)
                if not pt.poison and (pt.kind != "ptr" or pt.inner == VOID):
                    self.d.error(args[0].span, f"`write_at` expects a typed pointer, found `{pt}`")
                    self.check(args[1])
                elif pt.poison:
                    self.check(args[1])
                else:
                    self.expect(args[1], pt.inner)
            return VOID
        if name == "read_at":
            if arity(1):
                t = self.check(args[0], None)
                if t.poison:
                    return POISON
                if t.kind != "ptr" or t.inner == VOID:
                    self.d.error(args[0].span, f"`read_at` expects a typed pointer, found `{t}`")
                    return POISON
                return t.inner
            return POISON
        if name == "free":
            if arity(1):
                t = self.check(args[0], None)
                if not t.poison and t.kind != "slice" and t != STRING:
                    self.d.error(args[0].span, f"`free` expects a slice or a `String`, found `{t}`")
                elif t == STRING:
                    self.consume(args[0])       # freeing a String early moves it
                tr = self.taint(args[0])
                if tr is not None:
                    self.d.error(args[0].span, f"memory from region `{tr.name}` is freed when the region ends",
                                 help="remove the `free`")
            return VOID
        raise AssertionError(name)

    # ================================================================== errors
    def ex_Try(self, e, exp):
        t = self.check(e.expr, None, eu=True)
        if t.poison:
            return POISON
        if t.kind != "eu":
            self.d.error(e.span, f"`try` needs an expression that can fail, found `{t}`",
                         label="this cannot produce an error", help="`try` applies to calls to functions returning `T!`")
            return t
        if self.fn.try_stack:
            self.fn.try_stack[-1]["fails"].append(dict(self.moved))      # a failure goes to the enclosing `try { }` handler
        elif self.fn.ret.kind != "eu":
            self.d.error(e.span, "`try` can only be used in a function that returns an error union",
                         label=f"enclosing function returns `{self.fn.ret}`",
                         help=f"change the return type to `{self.fn.ret}!`, or handle the error with `catch`")
        return t.inner

    def ex_Catch(self, e, exp):
        t = self.check(e.expr, None, eu=True)
        h = e.handler
        if t.poison:
            self.check_handler_loosely(e)
            return POISON
        if t.kind != "eu":
            self.d.error(e.span, f"`catch` needs an expression that can fail, found `{t}`",
                         label="this cannot produce an error")
            self.check_handler_loosely(e)
            return t
        T = t.inner
        if h.kind == "Block":
            self.push()
            if e.binder:
                loc = self.declare_local(e.binder, ERR, False, e.binder_span)
                loc.used = True
                e.binder_local = loc
            else:
                e.binder_local = None
            saved_moved = dict(self.moved)
            self.check_block(h)
            if self.terminates(h):
                self.moved = saved_moved
            self.pop()
            if T != VOID and not self.terminates(h):
                self.d.error(h.span, f"`catch` block must not fall through: the call yields `{T}`",
                             label="block can complete normally",
                             help="end the block with `return`, `throw`, `break`, `continue` or `panic(...)`, "
                                  "or use `catch <default value>`")
        else:
            e.binder_local = None
            self.push()
            if e.binder:
                loc = self.declare_local(e.binder, ERR, False, e.binder_span)
                loc.used = True
                e.binder_local = loc
            if T == VOID:
                self.d.error(h.span, "a default value makes no sense here: the call yields no value",
                             help="use `catch { ... }` or `catch |e| { ... }`")
                self.check(h)
            else:
                self.expect(h, T)
            self.pop()
        return T

    def check_handler_loosely(self, e):
        h = e.handler
        if h.kind == "Block":
            self.push()
            if e.binder:
                loc = self.declare_local(e.binder, ERR, False, e.binder_span)
                loc.used = True
                e.binder_local = loc
            else:
                e.binder_local = None
            self.check_block(h)
            self.pop()
        else:
            self.check(h)

    # ============================================================== statements
    def check_block(self, b, scope=True):
        if scope:
            self.push()
        warned = False
        dead = False
        for s in b.stmts:
            if dead and not warned:
                self.d.warn(s.span, "unreachable statement", label="this code is never executed")
                warned = True
            self.check_stmt(s)
            if not dead and self.terminates(s):
                dead = True
        if scope:
            self.pop()

    def check_stmt(self, s):
        getattr(self, "st_" + s.kind)(s)

    def st_Block(self, s):
        self.check_block(s)

    def st_Let(self, s):
        ann = self.resolve_type(s.tyn, self.m) if s.tyn else None
        t = self.check(s.init, ann)
        # `let buf = alloc(T, n)`: a candidate for being freed automatically at the end of its scope (codegen checks it never escapes)
        s.afree = (s.init.kind == "Call" and getattr(s.init, "builtin", None) == "alloc" and not t.poison
                   and not self.has_glue(s.init.elem) and not self.rc)
        if ann == STR and t == STRING:
            s.init.coerce = True
            t = STR
        s.init.conv = None
        if ann is not None:
            if ann.kind == "opt" and not same(t, ann):
                self.coerce(s.init, t, ann)
            elif ann.is_numeric and t.is_numeric and t != ann:
                self.implicit_conv(s.init, t, ann)
            elif self.ro_violation(s.init, t, ann):
                pass
            elif self.ptr_ok(ann, t):
                self.consume(s.init)
            elif self.extra_conv(t, ann):
                self.coerce(s.init, t, ann)
            elif not same(t, ann):
                self.mismatch(s.init, ann, t)
            else:
                self.consume(s.init)
            ty = ann
        else:
            ty = t
            self.consume(s.init)
        if ty == VOID:
            self.d.error(s.init.span, "cannot bind a `void` value to a variable", label="this expression produces no value")
            ty = POISON
        if not ty.poison and not self.sized(ty, s.init.span):
            ty = POISON
        if s.name == "_":
            s.local = None
            s.drop_temp = (not ty.poison) and self.has_glue(ty)
            return
        s.local = self.declare_local(s.name, ty, s.mut, s.name_span)
        if not ty.poison and self.holds_ptr(ty):
            s.local.taint = self.taint(s.init)

    def cond(self, c):
        t = self.check(c, BOOL)
        if not same(t, BOOL):
            self.mismatch(c, BOOL, t, "condition must be a `bool`")

    def st_If(self, s):
        self.cond(s.cond)
        m0 = dict(self.moved)
        self.check_block(s.then)
        m_then = None if self.terminates(s.then) else self.moved
        self.moved = dict(m0)
        if s.els is not None:
            if s.els.kind == "If":
                self.st_If(s.els)
            else:
                self.check_block(s.els)
            m_else = None if self.terminates(s.els) else self.moved
        else:
            m_else = self.moved
        self.moved = self.merge_moved(m0, m_then, m_else)

    def st_While(self, s):
        def body():
            self.cond(s.cond)
            self.check_block(s.body)
        self.in_loop(s.body, body, True)

    def st_Loop(self, s):
        self.in_loop(s.body, lambda: self.check_block(s.body), False)

    def st_For(self, s):
        it = s.iter
        if it.kind == "Range":
            lo, hi = it.lo, it.hi
            if self.untyped(lo) and not self.untyped(hi):
                ht = self.check(hi, None)
                lt = self.check(lo, ht)
            else:
                lt = self.check(lo, None)
                ht = self.check(hi, lt)
            if not lt.poison and not ht.poison:
                if not lt.is_int:
                    self.d.error(lo.span, f"range bounds must be integers, found `{lt}`")
                    lt = POISON
                elif lt != ht:
                    lo.conv = hi.conv = None
                    if ht.is_int:
                        ct = self.common_type(lt, ht)
                        if ct is None or not (self.apply_conv(lo, lt, ct) and self.apply_conv(hi, ht, ct)):
                            if ct is None:
                                self.d.error(it.span, f"cannot mix `{lt}` and `{ht}` as range bounds",
                                             help="cast one bound explicitly")
                            lt = POISON
                        else:
                            lt = ct
                    else:
                        self.mismatch(hi, lt, ht, "range bounds must have the same type")
            vt = lt
            s.range_ty = lt
            s.mode = "range"
        else:
            ot = self.check(it, None)
            proto = self.iter_protocol(ot)
            if proto is not None:
                return self.desugar_for(s, it, proto)
            if ot.poison:
                vt = POISON
            elif ot.kind in ("arr", "slice"):
                vt = ot.inner
                if self.has_glue(vt) and not self.rc_copyable(vt):
                    self.d.error(it.span, f"cannot iterate over `{ot}` by value: `{vt}` has a destructor",
                                 help="iterate over indices (`for i in 0..xs.len`) and borrow with `&xs[i]`")
                    vt = POISON
            elif ot == STR or ot == STRING:
                vt = U8
            else:
                self.d.error(it.span, f"cannot iterate over a value of type `{ot}`",
                             help="iterate over arrays, slices, strings or a range like `0..n`")
                vt = POISON
            s.mode = "seq"

        def body():
            self.push()
            if s.var != "_":
                s.local = self.declare_local(s.var, vt, False, s.var_span)
                s.local.used = True
                if self.rc:
                    s.local.glue = False        # --rc: the loop variable borrows the element; the container still holds its count
            else:
                s.local = None
            self.check_block(s.body)
            self.pop()
        self.in_loop(s.body, body, True)

    def iter_protocol(self, ot):
        """How `for x in e` iterates a value of type `ot`: "next" (it is an iterator), "iter" (it has an `iter()` method), or None."""
        if ot.poison or ot.kind != "struct":
            return None
        tbl = self.methods.get(self.type_key(ot), {})
        nx = tbl.get("next")
        if nx is not None and nx.has_self:
            return "next"
        if "iter" in tbl and tbl["iter"].has_self:
            return "iter"
        return None

    def desugar_for(self, s, it, proto):
        """`for x in e { body }` over an iterator becomes
            { let mut it = e; loop { let o = it.next(); if o.is_none() { break; } let x = o.unwrap(); { body } } }
        so the iterator is destroyed on every way out of the loop by the ordinary scope rules."""
        sp = s.span
        ident = lambda n: N("Ident", sp, name=n)
        call = lambda obj, name: N("Call", sp, callee=N("Field", sp, obj=obj, name=name, name_span=sp), args=[], targ=None, targs=None)
        pre = []
        if proto == "next":
            src = it
        elif it.kind in ("Ident", "Field", "Index"):
            src = call(it, "iter")
        else:
            # a temporary (`for w in text.split(" ")`): keep it alive in a hidden local, since the iterator borrows it
            pre.append(N("Let", sp, name="cb_src", mut=False, tyn=None, init=it, name_span=sp))
            src = call(ident("cb_src"), "iter")
        var = s.var if s.var != "_" else "cb_x"
        body = N("Block", sp, stmts=[
            N("Let", sp, name="cb_o", mut=False, tyn=None, init=call(ident("cb_it"), "next"), name_span=sp),
            N("If", sp, cond=call(ident("cb_o"), "is_none"), then=N("Block", sp, stmts=[N("Break", sp)], end_span=sp), els=None),
            N("Let", sp, name=var, mut=False, tyn=None, init=call(ident("cb_o"), "unwrap"), name_span=s.var_span),
            s.body], end_span=sp)
        outer = N("Block", sp, stmts=pre + [
            N("Let", sp, name="cb_it", mut=True, tyn=None, init=src, name_span=sp),
            N("Loop", sp, body=body)], end_span=sp)
        s.desugared = outer
        self.check_block(outer)

    # ------------------------------------------------------------ regions (lifetimes)
    def st_Region(self, s):
        fn = self.fn
        fn.rdepth += 1
        self.push()
        loc = self.declare_local(s.name, REGION, False, s.name_span)
        loc.region_handle = True
        loc.used = True
        loc.glue = False
        loc.rd = fn.rdepth
        s.local = loc
        self.check_block(s.body)
        self.pop()
        fn.rdepth -= 1

    def holds_ptr(self, t):
        """Can a value of type `t` contain a pointer into region memory?"""
        k = t.kind
        if k == "prim":
            return t == REGION and False
        if k in ("enum", "err", "poison"):
            return False
        if k in ("arr", "opt"):
            return self.holds_ptr(t.inner)
        if k == "struct":
            return any(self.holds_ptr(ft) for _, ft, _ in t.sym.fields)
        if k == "eu":
            return self.holds_ptr(t.inner)
        return True                    # ptr, slice, fn

    def taint(self, e):
        """The innermost region whose memory the value of expression `e` may refer to (a handle Local), or None."""
        t = getattr(e, "ty", None)
        if t is None or t.poison or not self.holds_ptr(t):
            return None
        if e.kind == "Call" and getattr(e, "builtin", None) == "alloc_in":
            return e.region_local
        if e.kind == "Ident":
            loc = getattr(e, "local", None)
            return loc.taint if loc is not None else None
        if e.kind == "Unary" and e.op == "&":
            return self.place_taint(e.expr)         # the address of an element / field inside region memory
        best = None
        for k, v in e.__dict__.items():
            if k in ("span", "ty", "local", "sym", "handler"):
                continue
            kids = v if isinstance(v, (list, tuple)) else [v]
            for c in kids:
                if isinstance(c, N) and c.kind != "Block" and hasattr(c, "ty"):
                    tc = self.taint(c)
                    if tc is not None and (best is None or tc.rd > best.rd):
                        best = tc
        return best

    def place_taint(self, t):
        """The region that the storage of place `t` belongs to, if it is reached through a pointer or slice."""
        while t.kind in ("Field", "Index"):
            bt = getattr(t.obj, "ty", None)
            if bt is not None and (bt.kind in ("ptr", "slice") or bt in (STR, STRING)):
                return self.taint(t.obj)
            t = t.obj
        if t.kind == "Unary" and t.op == "*":
            return self.taint(t.expr)
        return None

    def place_depth(self, t):
        """Region nesting depth of the storage that assignment target `t` lives in (0 = outside every region)."""
        while True:
            k = t.kind
            if k == "Ident":
                loc = getattr(t, "local", None)
                return loc.rdepth if loc is not None else 0
            if k in ("Field", "Index"):
                base = t.obj
                bt = getattr(base, "ty", None)
                if bt is not None and (bt.kind in ("ptr", "slice") or bt in (STR, STRING)):
                    tb = self.taint(base)
                    return tb.rd if tb is not None else 0
                t = base
                continue
            if k == "Unary" and t.op == "*":
                tb = self.taint(t.expr)
                return tb.rd if tb is not None else 0
            return 0

    def escape_error(self, span, region, what):
        self.d.error(span, f"memory from region `{region.name}` {what}",
                     label=f"allocated with `alloc_in(.., {region.name})`, which is freed when the region ends",
                     help="copy what you need out of the region before it ends, or allocate it outside with `alloc`")

    def check_escape_store(self, target, value):
        tv = self.taint(value)
        if tv is None:
            return
        if tv.rd > self.place_depth(target):
            self.escape_error(value.span, tv, "would outlive the region if stored here")
        root = target
        while root.kind in ("Field", "Index"):
            root = root.obj
        if root.kind == "Ident" and getattr(root, "local", None) is not None and self.holds_ptr(root.local.ty):
            if root.local.taint is None or tv.rd > root.local.taint.rd:
                root.local.taint = tv

    def st_Return(self, s):
        ret = self.fn.ret
        T = ret.inner if ret.kind == "eu" else ret
        if s.expr is None:
            if T != VOID and not T.poison:
                self.d.error(s.span, f"`return` needs a value of type `{T}`", help=f"write `return <{T} value>;`")
            return
        if T == VOID:
            self.check(s.expr, None)
            self.d.error(s.expr.span, "unexpected return value: the function returns nothing",
                         help="remove the value, or give the function a return type with `-> T`")
            return
        self.expect(s.expr, T)
        x = s.expr
        tx = self.taint(x)
        if tx is not None:
            self.escape_error(x.span, tx, "cannot be returned")
        if T == STR and x.kind == "Ident" and getattr(x, "coerce", False) and getattr(x, "local", None) is not None \
                and x.local.glue:
            self.d.error(x.span, f"cannot return a view of the local `String` `{x.name}`: it is destroyed when the function returns",
                         label="this `str` would dangle",
                         help="return the `String` itself (change the return type to `String`)")

    def st_Break(self, s):
        if self.fn.loops == 0:
            self.d.error(s.span, "`break` outside of a loop")
        elif self.fn.loop_stack:
            self.fn.loop_stack[-1]["breaks"].append(dict(self.moved))

    def st_Continue(self, s):
        if self.fn.loops == 0:
            self.d.error(s.span, "`continue` outside of a loop")
        elif self.fn.loop_stack:
            self.fn.loop_stack[-1]["conts"].append(dict(self.moved))

    def st_Throw(self, s):
        if self.fn.try_stack:
            self.fn.try_stack[-1]["fails"].append(dict(self.moved))
        elif self.fn.ret.kind != "eu" and not self.fn.ret.poison:
            self.d.error(s.span, "`throw` can only be used in a function that returns an error union",
                         label=f"enclosing function returns `{self.fn.ret}`",
                         help=f"change the return type to `{self.fn.ret}!`")
        t = self.check(s.expr, ERR)
        if not same(t, ERR):
            self.d.error(s.expr.span, f"`throw` needs an error value, found `{t}`",
                         help="declare errors with `error Name;` and throw them by name")

    def st_ExprStmt(self, s):
        e = s.expr
        t = self.check(e, None, eu=(e.kind == "Call"))
        if e.kind == "Call" and t.kind == "eu" and self.fn.try_stack:
            t = self.auto_try(e, t)
        if e.kind == "Call" and t.kind == "eu":
            self.d.error(e.span, f"unused error from call returning `{t}`", label="this call can fail",
                         help="prefix with `try`, or handle it: `f() catch |e| { ... };`")
            return
        if e.kind not in ("Call", "Try", "Catch", "IncDec"):
            self.d.error(e.span, "expression statement has no effect", help="assign it to a variable or remove it")
        elif not t.poison and t != VOID and self.has_glue(t):
            s.drop_temp = True
            e.owned = True

    def st_Assign(self, s):
        t = s.target
        was_used = None
        if t.kind == "Ident":
            loc = self.lookup(t.name)
            if loc:
                was_used = loc.used
            if s.op == "=":
                self.reinit_ok = t          # re-assigning a moved variable is how it gets a new value
        tt = self.check(t, None)
        self.reinit_ok = None
        if was_used is not None and s.op == "=":
            t.local.used = was_used
        s.opty = tt
        if tt.poison:
            self.check(s.value, None)
            return
        if not self.is_place(t):
            self.d.error(t.span, "invalid left-hand side of assignment", label="cannot assign to this expression")
            self.check(s.value, None)
            return
        err = self.place_mut(t)
        if err:
            self.d.error(t.span, "cannot assign to an immutable place", label=err[0], help=err[1])
        if s.op == "=":
            self.expect(s.value, tt)
            self.check_escape_store(t, s.value)
            if t.kind == "Ident" and getattr(t, "local", None) is not None:
                self.moved.pop(t.local, None)
            return
        bop = s.op[:-1]
        s.bop = bop
        vt = self.check(s.value, tt if bop not in SHIFT else None)
        if vt.poison:
            return
        if bop in SHIFT:
            if not (tt.is_int and vt.is_int):
                self.d.error(s.op_span, f"cannot apply `{s.op}` to `{tt}` and `{vt}`")
            return
        if bop == "+" and tt in (STR, STRING) and (vt in (STR, STRING) or (tt == STRING and vt == U8)):
            return
        s.value.conv = None
        if tt.is_numeric and vt.is_numeric and vt != tt:
            if not self.implicit_conv(s.value, vt, tt):
                return
            vt = tt
        if not same(vt, tt):
            self.mismatch(s.value, tt, vt)
            return
        ok = (tt.is_numeric and not (bop == "%" and tt.is_float)) or (bop == "+" and tt == STR) if bop in ARITH else tt.is_int
        if not ok:
            self.d.error(s.op_span, f"cannot apply `{s.op}` to type `{tt}`")
        if bop in ("/", "%") and tt.is_int and s.value.kind == "Int" and s.value.value == 0:
            self.d.error(s.value.span, "division by zero")

    def st_Match(self, s):
        st = self.check(s.scrut, None)
        ok_t = st.poison or st.is_int or st == BOOL or st.kind == "enum"
        if not ok_t:
            self.d.error(s.scrut.span, f"cannot match on a value of type `{st}`",
                         help="match works on integers, `bool` and enums; use `if` otherwise")
            st = POISON
        seen = {}
        has_wild = False
        m0 = dict(self.moved)
        arm_states = []
        for arm in s.arms:
            self.moved = dict(m0)
            if arm.wild:
                has_wild = True
            for p in arm.pats:
                pt = self.check(p, st if not st.poison else None)
                if st.poison or pt.poison:
                    continue
                if not same(pt, st):
                    self.mismatch(p, st, pt, "pattern type does not match the matched value")
                    continue
                key = None
                if p.kind == "Field" and getattr(p, "mode", None) == "variant":
                    key = p.vidx
                elif p.kind in ("Int", "Bool") or (p.kind == "Unary" and p.expr.kind == "Int"):
                    try:
                        key = self.const_eval(p, self.m)
                    except NotConst:
                        key = None
                elif p.kind in ("Ident", "Field"):
                    try:
                        key = self.const_eval(p, self.m)
                    except NotConst:
                        self.d.error(p.span, "match patterns must be constants")
                        continue
                else:
                    self.d.error(p.span, "match patterns must be literals, constants or enum variants")
                    continue
                p.cval = key
                if key is not None:
                    if key in seen:
                        self.d.warn(p.span, "unreachable pattern: this value is already matched above")
                    seen[key] = 1
            self.check_block(arm.body)
            arm_states.append(None if self.terminates(arm.body) else self.moved)
        self.moved = self.merge_moved(m0, *arm_states) if arm_states else m0
        exhaustive = has_wild
        if not has_wild and not st.poison:
            if st.kind == "enum":
                missing = [v for i, v in enumerate(st.sym.variants) if i not in seen]
                if missing:
                    self.d.error(s.span, "non-exhaustive match",
                                 label=f"missing: {', '.join(st.sym.name + '.' + m for m in missing)}",
                                 help="add the missing variants, or a catch-all arm `_ => { ... }`")
                else:
                    exhaustive = True
            elif st == BOOL:
                if True in seen and False in seen:
                    exhaustive = True
                else:
                    self.d.error(s.span, "non-exhaustive match", label="`true` and `false` must both be covered",
                                 help="add the missing case or a `_` arm")
            else:
                self.d.error(s.span, "non-exhaustive match on an integer",
                             label="not all values are covered", help="add a catch-all arm `_ => { ... }`")
        s.exhaustive = exhaustive
        s.scrut_ty = st

    # =============================================================== analysis
    def has_break(self, n):
        if isinstance(n, N):
            if n.kind == "Break":
                return True
            if n.kind in ("While", "Loop", "For"):
                return False
            return any(self.has_break(v) for k, v in n.__dict__.items() if k not in ("span", "ty"))
        if isinstance(n, (list, tuple)):
            return any(self.has_break(x) for x in n)
        return False

    def terminates(self, s):
        k = s.kind
        if k in ("Return", "Throw", "Break", "Continue"):
            return True
        if k == "ExprStmt":
            e = s.expr
            return e.kind == "Call" and getattr(e, "builtin", None) in ("panic", "exit")
        if k == "Block":
            return any(self.terminates(x) for x in s.stmts)
        if k == "Region":
            return self.terminates(s.body)
        if k == "TryBlock":
            return self.terminates(s.body) and self.terminates(s.handler)
        if k == "If":
            return s.els is not None and self.terminates(s.then) and self.terminates(s.els)
        if k == "Match":
            return bool(getattr(s, "exhaustive", False)) and bool(s.arms) and all(self.terminates(a.body) for a in s.arms)
        if k == "Loop":
            return not self.has_break(s.body)
        if k == "While":
            return s.cond.kind == "Bool" and s.cond.value and not self.has_break(s.body)
        return False
