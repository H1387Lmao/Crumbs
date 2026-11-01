"""Semantic analysis: name resolution, type checking, mutability, control-flow checks.

The checker annotates the AST in place (node.ty, node.local, node.sym, ...) so the
code generator never has to re-derive types.
"""
import copy
import re
from .timing import TIMINGS
from .diag import suggest
from .parser import N
from .types import (INTS, PRIMS, Ty, I8, I16, I32, I64, U8, U16, U32, U64, F32, F64, BOOL, STR, VOID,
                    ERR, POISON, STRING, REGION, ptr, arr, slc, eu, fnty, same)

BUILTINS = {"print", "println", "panic", "assert", "alloc", "free", "exit", "to_str",
            "string_new", "string_from", "string_clear",
            "cstr", "from_cstr", "slice_of", "size_of", "drop", "drop_at", "write_at", "read_at",
            "gc_collect", "gc_bytes", "gc_collections", "alloc_in"}
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
    def __init__(self, mods, diags, c_reserved=(), strict=False, entry_name="main", nostd=False):
        self.strict = strict
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
        self.n_inst = 0
        self.glue = {}
        self.glue_busy = set()
        self.methods = {}        # type key -> {method name: Sym}
        self.prelude_types = {}  # names usable without an import (`List`)
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
        for m in self.mods:
            self.resolve_sigs(m)
        self.check_ffi()
        for m in self.mods:
            self.check_consts(m)
        for m in self.mods:
            self.check_bodies(m)
        while self.pending:
            self.check_fn_body(self.pending.pop(0))
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
        for fn in it.fns:
            if fn.name in table:
                old = table[fn.name]
                self.d.error(fn.span, f"`{tname}` already has a method named `{fn.name}`",
                             notes=[f"the first one is at line {old.node.span.line}"])
                continue
            node = copy.copy(fn)
            node.name = f"{tname}.{fn.name}"
            node.tparams = list(it.tparams) + list(fn.tparams)
            s = Sym("fn", node.name, m, node, fn.pub)
            s.tparams = node.tparams
            s.cname = f"{m.cprefix}__{tname}__m_{fn.name}"
            s.is_method = True
            s.method_name = fn.name
            s.has_self = bool(fn.params) and fn.params[0].name == "self"
            s.self_mut = s.has_self and getattr(fn.params[0], "self_mut", False)
            s.owner_key = key
            if self.define(m, s, fn.span):
                table[fn.name] = s
                if not s.tparams:
                    self.fns.append(s)

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
            return POISON if inner.poison else ptr(inner)
        if k == "TFn":
            ps = [self.resolve_type(p, m) for p in tn.params]
            ret = self.resolve_type(tn.ret, m, allow_void=True) if tn.ret is not None else VOID
            if ret.poison or any(p.poison for p in ps):
                return POISON
            return fnty(ps, ret)
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
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
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
        if s.base is not None:
            binds = ", ".join(f"{k} = {v}" for k, v in s.tenv.items())
            self.d.context = f"while checking `{s.base.name}` instantiated with {binds}"
        self.fn = FnInfo(s)
        self.scopes = [{}]
        self.moved = {}
        s.param_locals = []
        for p, t in zip(it.params, s.params):
            s.param_locals.append(self.declare_local(p.name, t, getattr(p, "mut", False), p.name_span, is_param=True))
        self.check_block(it.body)
        self.pop()
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
        elif k == "TFn" and ty.kind == "fn" and len(tn.params) == len(ty.params):
            for a, p in zip(tn.params, ty.params):
                self.unify(a, p, sym, b)
            if tn.ret is not None:
                self.unify(tn.ret, ty.inner, sym, b)

    def is_bare_param(self, tn, sym, b):
        return tn.kind == "TName" and len(tn.path) == 1 and tn.path[0] in sym.tparams and tn.path[0] in b

    def report_uninferred(self, span, sym, b, what):
        missing = [n for n in sym.tparams if n not in b]
        self.d.error(span, f"cannot infer type parameter{'s' if len(missing) > 1 else ''} "
                     f"{', '.join('`' + m + '`' for m in missing)} of `{sym.name}`",
                     label="type annotations needed",
                     help=f"specify explicitly, e.g. `{sym.name}<{', '.join(sym.tparams)}>(...)`"
                     if what == "fn" else "annotate the variable or give the fields concrete values")

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
                if not self.untyped(a):
                    self.unify(p.tyn, self.check(a, None), sym, b)
            for a, p in zip(e.args, params):
                if self.untyped(a) and not self.is_bare_param(p.tyn, sym, b):
                    self.unify(p.tyn, self.check(a, None), sym, b)
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

    def has_glue(self, t):
        """Does dropping a value of type `t` run any destructor?"""
        k = t.kind
        if t == STRING:
            return True                 # an owned String frees its buffer when it goes out of scope
        if k == "arr":
            return self.has_glue(t.inner)
        if k != "struct":
            return False
        key = t.key
        if key in self.glue:
            return self.glue[key]
        if key in self.glue_busy:
            return False
        self.glue_busy.add(key)
        sym = t.sym
        r = self.drop_fn_of(sym) is not None or any(self.has_glue(ft) for _, ft, _ in sym.fields)
        self.glue_busy.discard(key)
        self.glue[key] = r
        return r

    def consume(self, e):
        """`e` is used by value where ownership is taken: a variable is moved, anything else must be a temporary."""
        t = getattr(e, "ty", None)
        if t is None or t.poison or not self.has_glue(t) or getattr(e, "coerce", False):
            return                      # (a `String` converted to a `str` view is a borrow, not a move)
        k = e.kind
        if k == "Ident" and getattr(e, "local", None) is not None:
            e.move = True
            self.moved[e.local] = e.span
            return
        if k in ("Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch"):
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

    def ptr_ok(self, exp, t):
        """`*T` converts implicitly to `*void` (as in C), never the other way round."""
        return exp.kind == "ptr" and exp.inner == VOID and t.kind == "ptr"

    def expect(self, e, exp, eu_ok=False):
        t = self.check(e, exp, eu=eu_ok)
        e.conv = None
        if exp == STR and t == STRING:
            e.coerce = True
            return exp
        if t.is_numeric and exp.is_numeric and t != exp:
            return exp if self.implicit_conv(e, t, exp) else POISON
        if self.ptr_ok(exp, t):
            return exp
        if not same(t, exp):
            self.mismatch(e, exp, t)
            return POISON
        self.consume(e)
        return t

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

    def check(self, e, exp=None, eu=False):
        t = getattr(self, "ex_" + e.kind)(e, exp)
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

    # ================================================================ names
    def ex_Ident(self, e, exp):
        if e.name == "null" and not self.lookup("null"):
            e.is_null = True
            if exp is not None and exp.kind in ("ptr", "fn"):
                return exp
            if exp is not None and exp.poison:
                return POISON
            self.d.error(e.span, "cannot infer the pointer type of `null`",
                         help="write `null as *T`, or give the variable a pointer type, e.g. `let p: *i32 = null;`")
            return POISON
        loc = self.lookup(e.name)
        if loc:
            loc.used = True
            e.local = loc
            if loc.region_handle and not getattr(self, "region_ok", False):
                self.d.error(e.span, f"the region `{e.name}` can only be used as the last argument of `alloc_in`",
                             help=f"write `alloc_in(T, n, {e.name})`")
                return POISON
            if loc in self.moved and e is not self.reinit_ok:
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
            what = {"fn": "function", "struct": "struct", "enum": "enum"}[sym.kind]
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
                    "Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch", "Cast", "Field", "Index"):
                e.temp = True                   # `make().method()`: the receiver is a temporary that lives for the statement
                return ptr(t)
            if not self.is_place(e.expr):
                self.d.error(e.span, "cannot take the address of a temporary value",
                             help="store it in a `let mut` variable first")
                return POISON
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

    def place_mut(self, e):
        """None if `e` is a mutable place; otherwise (label, help)."""
        k = e.kind
        if k == "Ident":
            loc = e.local
            if loc.mut:
                return None
            if loc.is_param:
                return (f"`{loc.name}` is a parameter, and parameters are immutable by default",
                        f"declare it as `mut {loc.name}: {loc.ty}` in the parameter list")
            return (f"`{loc.name}` is declared without `mut`", f"declare it with `let mut {loc.name}`")
        if k == "Field":
            if e.mode == "ptrfield":
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
            return None
        return ("not an assignable place", None)

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
            lt = self.check(l, rt if (rt.is_numeric or rt.kind in ("ptr", "fn")) else None)
        else:
            lt = self.check(l, hint)
            rt = self.check(r, lt if (lt.is_numeric or lt.kind in ("ptr", "fn")) else None)
        e.opty = lt
        if lt.poison or rt.poison:
            return BOOL if op in CMP else POISON
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
        for x in e.elems[1:]:
            t = self.check(x, first if not first.poison else elem_exp)
            if not same(t, first):
                self.mismatch(x, first, t, "array elements must all have the same type")
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
        if not t.poison and self.has_glue(t):
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
        if t.kind != "fn":
            self.d.error(c.span, f"cannot call a value of type `{t}`", label="not a function",
                         help="only functions and function pointers (`fn(A) -> R`) can be called")
            for a in e.args:
                self.check(a)
            return POISON
        e.indirect = True
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
                return obj                                          # already a pointer
            if cand != base:                                        # String -> str needs a by-value view, not a pointer
                return obj
            if not self.is_place(obj) and getattr(obj, "kind", "") != "Ident":
                pass
            node = N("Unary", obj.span, op="&", expr=obj)
            node.auto_ref = not meth.self_mut                       # `self` may borrow immutable bindings; `mut self` may not
            return node
        if ot.kind == "ptr":
            return N("Unary", obj.span, op="*", expr=obj)
        return obj

    PRINTABLE_KINDS = ("enum", "ptr", "fn")

    NOSTD_BANNED = {"print", "println", "alloc", "free", "exit", "to_str", "string_new", "string_from",
                    "string_clear", "cstr", "gc_collect", "gc_bytes", "gc_collections", "alloc_in"}

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
            for a in args:
                t = self.check(a, None)
                if t.poison:
                    continue
                if not (t.is_numeric or t in (BOOL, STR, STRING, ERR) or t.kind in self.PRINTABLE_KINDS):
                    self.d.error(a.span, f"cannot print a value of type `{t}`",
                                 help="print fields individually" if t.kind in ("struct", "arr", "slice") else None)
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
                if not t.poison and not (t.is_numeric or t in (BOOL, STR, STRING, ERR) or t.kind == "enum"):
                    self.d.error(args[0].span, f"cannot convert a value of type `{t}` to `str`")
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
                self.consume(args[0])
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
            if T == VOID:
                self.d.error(h.span, "a default value makes no sense here: the call yields no value",
                             help="use `catch { ... }` or `catch |e| { ... }`")
                self.check(h)
            else:
                self.expect(h, T)
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
        if ann == STR and t == STRING:
            s.init.coerce = True
            t = STR
        s.init.conv = None
        if ann is not None:
            if ann.is_numeric and t.is_numeric and t != ann:
                self.implicit_conv(s.init, t, ann)
            elif self.ptr_ok(ann, t):
                pass
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
            if ot.poison:
                vt = POISON
            elif ot.kind in ("arr", "slice"):
                vt = ot.inner
                if self.has_glue(vt):
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
            else:
                s.local = None
            self.check_block(s.body)
            self.pop()
        self.in_loop(s.body, body, True)

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
        if k == "arr":
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
