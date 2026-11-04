"""Recursive-descent parser with panic-mode error recovery."""
import time
from .timing import TIMINGS
from .diag import Span, Source
from .lexer import lex, Token
from .types import PRIMS


class N:
    """Generic AST node: `kind`, `span`, plus arbitrary attributes."""

    def __init__(self, kind, span, **kw):
        self.kind = kind
        self.span = span
        self.__dict__.update(kw)

    def __repr__(self):
        items = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items() if k not in ("kind", "span"))
        return f"{self.kind}({items})"


class ParseAbort(Exception):
    pass


BIN_PREC = {
    "??": 1, "||": 2, "&&": 3,
    "==": 4, "!=": 4, "<": 4, ">": 4, "<=": 4, ">=": 4,
    "|": 5, "^": 6, "&": 7, "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10, "*%": 10,
}
CMP_OPS = {"==", "!=", "<", ">", "<=", ">="}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
ITEM_START = {"fn", "struct", "enum", "const", "import", "pub", "extern", "error", "export"}
TYPE_ARG_BUILTINS = {"alloc", "alloc_in"}
TYPE_ONLY_BUILTINS = {"size_of"}
DIRECTIVES = {"link", "include", "define"}


class Parser:
    def __init__(self, src, diags):
        self.src = src
        self.d = diags
        t0 = time.perf_counter()
        with TIMINGS.phase("lex"):
            self.toks = lex(src, diags)
        self.lex_time = time.perf_counter() - t0
        self.p = 0
        self.no_struct = False
        self.undo = None
        self.uses_async = False
        self.impl_ty = None          # type node of the `impl` block being parsed (for `self` / `Self`)
        self.tbounds = {}

    # ------------------------------------------------------------ helpers
    @property
    def tok(self):
        return self.toks[self.p]

    def peek(self, k=1):
        return self.toks[min(self.p + k, len(self.toks) - 1)]

    @property
    def prev(self):
        return self.toks[max(0, self.p - 1)]

    def advance(self):
        t = self.toks[self.p]
        if t.kind != "eof":
            self.p += 1
        return t

    def is_op(self, v, k=0):
        t = self.peek(k) if k else self.tok
        return t.kind == "op" and t.val == v

    def is_kw(self, v, k=0):
        t = self.peek(k) if k else self.tok
        return t.kind == "kw" and t.val == v

    def accept_op(self, v):
        if self.is_op(v):
            return self.advance()
        return None

    def accept_kw(self, v):
        if self.is_kw(v):
            return self.advance()
        return None

    def describe(self, t):
        if t.kind == "eof":
            return "end of file"
        if t.kind == "str":
            return "string literal"
        if t.kind in ("int", "float", "char"):
            return "literal"
        if t.kind == "kw":
            return f"keyword `{t.val}`"
        return f"`{t.val}`"

    def fail(self, msg, tok=None, label=None, help=None):
        t = tok or self.tok
        if not self.d.has_error_on(t.span):   # avoid cascades from an earlier lexer/parse error on this line
            self.d.error(t.span, msg, label=label, help=help)
        raise ParseAbort()

    def expect_op(self, v, ctx=None):
        if self.is_op(v):
            return self.advance()
        where = f" {ctx}" if ctx else ""
        # when a `;` or `)` is missing, point at the end of the previous token
        if v in (";", ")", "]", "}") and self.p > 0 and self.tok.span.line != self.prev.span.line:
            ps = self.prev.span
            if not self.d.has_error_on(ps):
                self.d.error(Span(ps.src, ps.line, ps.col + ps.length, 1), f"expected `{v}`{where}",
                             label=f"expected `{v}` here")
            raise ParseAbort()
        self.fail(f"expected `{v}`{where}, found {self.describe(self.tok)}", label=f"expected `{v}`")

    def expect_gt(self):
        """Consume a `>`; splits a `>>` token so nested generics like `A<B<C>>` parse."""
        if self.is_op(">"):
            return self.advance()
        if self.is_op(">>"):
            t = self.tok
            if self.undo is not None:
                self.undo.append((self.p, t))
            sp = t.span
            from .diag import Span
            self.toks[self.p] = Token("op", ">", Span(sp.src, sp.line, sp.col + 1, 1))
            return t
        self.fail(f"expected `>`, found {self.describe(self.tok)}", label="expected `>`")

    def parse_tparams(self):
        """`<A, B: Trait + Other>`: returns the names; the bounds are left in `self.tbounds` for the caller."""
        names = []
        self.tbounds = {}
        if self.accept_op("<"):
            while True:
                t = self.expect_ident("type parameter name")
                if t.val in names:
                    self.fail(f"type parameter `{t.val}` is declared more than once", tok=t)
                names.append(t.val)
                if self.accept_op(":"):
                    self.tbounds[t.val] = self.parse_bound_list()
                if not self.accept_op(","):
                    break
            self.expect_gt()
        return names

    def parse_bound_list(self):
        """`Trait + module.Other`: a list of trait paths (used for bounds and supertraits)."""
        bounds = []
        while True:
            t = self.expect_ident("trait name")
            path = [t.val]
            if self.is_op(".") and self.peek().kind == "ident":
                self.advance()
                path.append(self.advance().val)
            if self.is_op("<"):
                self.fail("traits are not generic: a bound is just the trait's name", label="remove the type arguments")
            bounds.append(N("TName", self.sp(t), path=path, args=[]))
            if not self.accept_op("+"):
                break
        return bounds

    def parse_type_args(self):
        args = [self.parse_type()]
        while self.accept_op(","):
            args.append(self.parse_type())
        self.expect_gt()
        return args

    def expect_kw(self, v):
        if self.is_kw(v):
            return self.advance()
        self.fail(f"expected keyword `{v}`, found {self.describe(self.tok)}")

    def expect_ident(self, what="identifier"):
        t = self.tok
        if t.kind == "ident":
            return self.advance()
        if t.kind == "kw":
            self.fail(f"expected {what}, found keyword `{t.val}`", label=f"`{t.val}` is a reserved word")
        self.fail(f"expected {what}, found {self.describe(t)}", label=f"expected {what}")

    def sp(self, start_tok):
        return start_tok.span.join(self.prev.span)

    # ----------------------------------------------------------- recovery
    def sync_stmt(self):
        """Skip to a plausible statement boundary."""
        depth = 0
        while self.tok.kind != "eof":
            t = self.tok
            if t.kind == "op":
                if t.val in "([{" and len(t.val) == 1:
                    depth += 1
                elif t.val in ")]" and depth > 0:
                    depth -= 1
                elif t.val == "}":
                    if depth == 0:
                        return
                    depth -= 1
                elif t.val == ";" and depth == 0:
                    self.advance()
                    return
            elif t.kind == "kw" and depth == 0 and t.val in (
                "let", "if", "while", "loop", "for", "return", "break", "continue", "match", "throw"
            ):
                return
            self.advance()

    def sync_item(self):
        depth = 0
        first = True
        while self.tok.kind != "eof":
            t = self.tok
            if t.kind == "op" and t.val == "{":
                depth += 1
            elif t.kind == "op" and t.val == "}":
                depth = max(0, depth - 1)
            elif t.kind == "kw" and depth == 0 and t.val in ITEM_START and not first:
                return
            first = False
            self.advance()

    # -------------------------------------------------------------- items
    def parse_module(self):
        items = []
        while self.tok.kind != "eof":
            start = self.p
            try:
                it = self.parse_item()
                if isinstance(it, list):
                    items.extend(it)
                elif it:
                    items.append(it)
            except ParseAbort:
                self.sync_item()
                if self.p == start:
                    self.advance()
        if self.uses_async and not any(i.kind == "Import" and i.path == ["std", "async"] for i in items):
            sp = Span(self.src, 1, 1, 1)
            items.insert(0, N("Import", sp, path=["std", "async"], alias=None, name_span=sp))
        return N("Module", Span(self.src, 1, 1, 1), items=items)

    def is_directive(self):
        t, n = self.tok, self.peek()
        if t.kind != "ident" or t.val not in DIRECTIVES:
            return False
        if n.kind == "str":
            return True
        return t.val == "link" and n.kind == "ident" and n.val == "pkg" and self.peek(2).kind == "str"

    def parse_directive(self):
        start = self.advance()
        what = start.val
        if what == "link" and self.tok.kind == "ident" and self.tok.val == "pkg":
            self.advance()
            what = "pkg"
        s = self.tok
        if s.kind != "str":
            self.fail(f"expected a string after `{start.val}`", label="expected a string literal")
        self.advance()
        try:
            value = s.val.decode("utf-8")
        except UnicodeDecodeError:
            self.fail("directive strings must be valid UTF-8", tok=s)
        self.expect_op(";", f"after `{start.val}` directive")
        return N("Directive", self.sp(start), what=what, value=value, vspan=s.span)

    def parse_drop(self, pub):
        start = self.advance()                      # `drop`
        tparams = self.parse_tparams()
        if self.tbounds:
            self.fail("a destructor cannot put trait bounds on its type parameters", tok=start,
                      label="bounds belong on the struct's declaration")
        ty = self.parse_type()
        if ty.kind != "TName" or len(ty.path) != 1:
            self.fail("a destructor is declared as `drop TypeName(self) { ... }`", tok=start,
                      label="expected the name of a struct declared in this module")
        self.expect_op("(", "after the destructor's type")
        me = self.expect_ident("name for the value being destroyed")
        self.expect_op(")", "after the destructor parameter")
        body = self.parse_block()
        self_ty = N("TPtr", ty.span, inner=ty)
        param = N("Param", me.span, name=me.val, tyn=self_ty, name_span=me.span, mut=False)
        return N("Fn", ty.span, name=f"cb_drop_{ty.path[0]}", params=[param], ret=None, body=body, pub=False,
                 extern=False, tparams=tparams, full_span=self.sp(start), export=False, cname_str=None,
                 variadic=False, drop_for=ty.path[0])

    def parse_impl(self):
        start = self.advance()                      # `impl`
        tparams = self.parse_tparams()
        tbounds = self.tbounds
        ty = self.parse_type()
        trait = None
        if self.accept_kw("for"):                   # `impl Trait for Type { ... }`
            if ty.kind != "TName" or len(ty.path) > 2 or ty.args:
                self.fail("expected a trait name before `for`", tok=start, label="`impl Trait for Type`")
            trait = ty
            ty = self.parse_type()
        if ty.kind != "TName":
            self.fail("methods can only be declared for a named type: `impl Point { ... }`", tok=start,
                      label="expected a struct, enum or built-in type name")
        self.expect_op("{", "after the type of an `impl` block")
        saved, self.impl_ty = self.impl_ty, ty
        fns = []
        while not self.is_op("}"):
            if self.tok.kind == "eof":
                self.fail("unclosed `impl` block", tok=start, label="this block is never closed")
            pub = bool(self.accept_kw("pub"))
            if not self.is_kw("fn"):
                self.fail(f"only functions can be declared inside `impl`, found {self.describe(self.tok)}",
                          label="expected `fn`")
            fns.append(self.parse_fn(pub, extern=False))
        self.expect_op("}")
        self.impl_ty = saved
        return N("Impl", self.sp(start), tparams=tparams, tbounds=tbounds, tyn=ty, fns=fns, trait=trait)

    def parse_trait(self, pub):
        start = self.advance()                      # `trait`
        nm = self.expect_ident("trait name")
        supers = []
        if self.accept_op(":"):
            supers = self.parse_bound_list()
        self.expect_op("{", "after the trait name")
        saved, self.impl_ty = self.impl_ty, N("TName", nm.span, path=["Self"], args=[])   # `Self` stays symbolic
        methods = []
        while not self.is_op("}"):
            if self.tok.kind == "eof":
                self.fail("unclosed `trait` block", tok=start, label="this block is never closed")
            if self.is_kw("pub"):
                self.fail("trait methods cannot be marked `pub`: they are as visible as the trait itself")
            if not self.is_kw("fn"):
                self.fail(f"only functions can be declared inside `trait`, found {self.describe(self.tok)}",
                          label="expected `fn`")
            methods.append(self.parse_trait_fn())
        self.expect_op("}")
        self.impl_ty = saved
        return N("Trait", nm.span, name=nm.val, pub=pub, supers=supers, methods=methods, full_span=self.sp(start))

    def parse_trait_fn(self):
        """`fn name(self, ...) -> T;` (required) or `fn name(self, ...) -> T { ... }` (a default method).
        A default body is kept as tokens: each `impl` that uses it parses them again with `Self` bound to its own type."""
        first = self.p
        start = self.expect_kw("fn")
        nm = self.expect_ident("function name")
        if self.is_op("<"):
            self.fail("trait methods cannot be generic", tok=nm, label="declare the type parameters on the trait's users instead")
        params, _ = self.parse_params()
        ret = self.parse_ret_type() if self.accept_op("->") else None
        body = toks = None
        if not self.accept_op(";"):
            body = self.parse_block()
            toks = self.toks[first:self.p]
        return N("TraitFn", nm.span, name=nm.val, params=params, ret=ret, body=body, toks=toks, eof=self.toks[-1],
                 src=self.src, full_span=self.sp(start))

    def parse_item(self):
        start = self.tok
        pub = bool(self.accept_kw("pub"))
        t = self.tok
        if not pub and self.is_directive():
            return self.parse_directive()
        if t.kind == "ident" and t.val == "drop" and (self.peek().kind == "ident" or self.peek().kind == "op" and self.peek().val == "<"):
            return self.parse_drop(pub)
        if t.kind == "ident" and t.val == "trait" and self.peek().kind == "ident":
            return self.parse_trait(pub)
        if t.kind == "ident" and t.val == "impl" and (self.peek().kind == "ident" or self.peek().kind == "op" and self.peek().val == "<"):
            if pub:
                self.fail("`pub` is not allowed on `impl` blocks; mark the functions inside as `pub`", tok=start)
            return self.parse_impl()
        if t.kind == "ident" and t.val == "async" and self.is_kw("fn", 1):
            self.advance()
            return self.parse_async_fn(pub)
        if t.kind != "kw":
            self.fail(f"expected an item (`fn`, `struct`, `enum`, `trait`, `const`, `error`, `import`, `impl`), found {self.describe(t)}",
                      label="not a valid top-level item")
        v = t.val
        if v == "import":
            if pub:
                self.fail("`pub` is not allowed on imports", tok=start)
            return self.parse_import()
        if v == "fn":
            return self.parse_fn(pub, extern=False)
        if v == "export":
            self.advance()
            if not self.is_kw("fn"):
                self.fail("`export` applies to functions: `export fn name(...) { ... }`")
            return self.parse_fn(pub, extern=False, export=True)
        if v == "extern":
            self.advance()
            cname = None
            if self.tok.kind == "str":
                cname = self.advance().val.decode("utf-8", "replace")
            if self.is_kw("struct"):
                return self.parse_struct(pub, extern=True, cname=cname)
            if self.is_kw("const"):
                return self.parse_const(pub, extern=True, cname=cname)
            if not self.is_kw("fn"):
                self.fail("expected `fn`, `struct` or `const` after `extern`", label="expected a C declaration")
            return self.parse_fn(pub, extern=True, cname=cname)
        if v == "struct":
            return self.parse_struct(pub)
        if v == "enum":
            return self.parse_enum(pub)
        if v == "const":
            return self.parse_const(pub)
        if v == "error":
            return self.parse_error_decl(pub)
        self.fail(f"expected an item, found {self.describe(t)}")

    def type_text(self, tn):
        """Source text of a parsed type (used to generate the helpers of an `async fn`)."""
        k = tn.kind
        if k == "TName":
            return ".".join(tn.path) + (f"<{', '.join(self.type_text(a) for a in tn.args)}>" if tn.args else "")
        if k == "TPtr":
            return "*" + self.type_text(tn.inner)
        if k == "TSlice":
            return "[]" + self.type_text(tn.inner)
        if k == "TArray":
            n = getattr(tn.size, "value", None)
            if n is None:
                self.fail("an async function's parameter types must use plain array lengths", tok=self.prev)
            return f"[{n}]" + self.type_text(tn.inner)
        if k == "TErrUnion":
            return self.type_text(tn.inner) + "!"
        if k == "TOpt":
            return self.type_text(tn.inner) + "?"
        if k == "TFn":
            ret = f" -> {self.type_text(tn.ret)}" if tn.ret is not None else ""
            return f"fn({', '.join(self.type_text(x) for x in tn.params)}){ret}"
        self.fail("this type cannot be used in an async function", tok=self.prev)

    def parse_async_fn(self, pub):
        """`async fn f(a: A) -> R { body }` becomes the ordinary function `f__body`, plus a generated argument struct,
        a thread entry point and `f(a: A)` itself, which starts the body on a new thread and returns a task."""
        fn = self.parse_fn(pub, extern=False)
        name = fn.name
        if fn.tparams:
            self.fail("async functions cannot be generic", tok=self.prev)
        if any(getattr(p, "self_param", False) for p in fn.params):
            self.fail("async methods are not supported: write an async function that takes the value as a parameter", tok=self.prev)
        self.uses_async = True
        fn.name = name + "__body"
        fn.pub = False
        ret = fn.ret
        fallible = ret is not None and ret.kind == "TErrUnion"
        inner = ret.inner if fallible else ret
        void = inner is None or (inner.kind == "TName" and inner.path == ["void"])
        params = [(p.name, self.type_text(p.tyn)) for p in fn.params]
        fields = "".join(f"{n}: {t}, " for n, t in params)
        sig = ", ".join(f"{n}: {t}" for n, t in params)
        inits = "".join(f"{n}: {n}, " for n, _ in params)
        call = f"{name}__body(" + ", ".join(f"read_at(&a.{n})" for n, _ in params) + ")"
        if void and not fallible:
            kind, ctor, store = "Job", "async.new_job()", f"{call};"
        elif void:
            kind, ctor = "TryJob", "async.new_try_job()"
            store = f"{call}.catch(|e| {{ a.task.failed = true; a.task.err = e; return null; }});"
        else:
            tt = self.type_text(inner)
            if not fallible:
                kind, ctor, store = f"Task<{tt}>", f"async.new_task<{tt}>()", f"a.task.value = {call};"
            else:
                kind, ctor = f"TryTask<{tt}>", f"async.new_try_task<{tt}>()"
                store = (f"let v = {call}.catch(|e| {{ a.task.failed = true; a.task.err = e; return null; }});\n"
                         f"    a.task.value = v;")
        text = (f"struct {name}__Args {{ {fields}task: *async.{kind} }}\n"
                f"fn {name}__entry(p: *void) -> *void {{\n"
                f"    let a = p as *{name}__Args;\n"
                f"    {store}\n"
                f"    return null;\n}}\n"
                f"{'pub ' if pub else ''}fn {name}({sig}) -> *async.{kind} {{\n"
                f"    let task = {ctor};\n"
                f"    let args = alloc({name}__Args, 1);\n"
                f"    args[0] = {name}__Args {{ {inits}task: task }};\n"
                f"    task.start({name}__entry, &args[0] as *void);\n"
                f"    return task;\n}}\n")
        sub = Parser(Source(f"<async fn {name}>", text), self.d)
        gen = sub.parse_module().items
        return [fn] + gen

    def parse_import(self):
        start = self.advance()
        parts = [self.expect_ident("module name")]
        while self.accept_op("."):
            parts.append(self.expect_ident("module name"))
        alias = None
        if self.accept_kw("as"):
            alias = self.expect_ident("alias")
        self.expect_op(";", "after import")
        return N("Import", self.sp(start), path=[p.val for p in parts], alias=alias.val if alias else None,
                 name_span=parts[-1].span if not alias else alias.span)

    def parse_params(self, allow_variadic=False):
        self.expect_op("(")
        params = []
        variadic = False
        while not self.is_op(")"):
            if self.is_op("..."):
                if not allow_variadic:
                    self.fail("only `extern fn` declarations can be variadic", label="`...` is for C functions like `printf`")
                self.advance()
                variadic = True
                break
            is_mut = bool(self.accept_kw("mut"))
            nm = self.expect_ident("parameter name")
            if nm.val == "self" and not self.is_op(":"):
                if self.impl_ty is None or params:
                    self.fail("`self` can only be the first parameter of a function inside an `impl` block", tok=nm)
                params.append(N("Param", nm.span, name="self", tyn=N("TPtr", nm.span, inner=self.impl_ty),
                                name_span=nm.span, mut=False, self_param=True, self_mut=is_mut))
                if not self.accept_op(","):
                    break
                continue
            self.expect_op(":", "after parameter name")
            ty = self.parse_type()
            default = None
            if self.accept_op("="):                                  # `port: i32 = 80`
                saved = self.no_struct
                self.no_struct = False
                default = self.parse_expr()
                self.no_struct = saved
            elif params and getattr(params[-1], "default", None) is not None:
                self.fail(f"parameter `{nm.val}` has no default, but the one before it does",
                          tok=nm, help="parameters with defaults must come last")
            params.append(N("Param", nm.span.join(self.prev.span), name=nm.val, tyn=ty, name_span=nm.span, mut=is_mut, default=default))
            if not self.accept_op(","):
                break
        self.expect_op(")", "after parameters")
        return params, variadic

    def parse_fn(self, pub, extern, cname=None, export=False):
        start = self.expect_kw("fn")
        nm = self.expect_ident("function name")
        tparams = self.parse_tparams()
        tbounds = self.tbounds
        if extern and tparams:
            self.fail("extern functions cannot be generic", tok=nm)
        if export and tparams:
            self.fail("exported functions cannot be generic", tok=nm)
        params, variadic = self.parse_params(allow_variadic=extern)
        ret = None
        if self.accept_op("->"):
            ret = self.parse_ret_type()
        body = None
        if extern:
            self.expect_op(";", "after extern function declaration")
        else:
            body = self.parse_block()
        return N("Fn", nm.span, name=nm.val, params=params, ret=ret, body=body, pub=pub, extern=extern,
                 tparams=tparams, tbounds=tbounds, full_span=self.sp(start), export=export, cname_str=cname,
                 variadic=variadic, drop_for=None)

    def parse_struct(self, pub, extern=False, cname=None):
        self.advance()
        nm = self.expect_ident("struct name")
        tparams = self.parse_tparams()
        tbounds = self.tbounds
        if extern and tparams:
            self.fail("extern structs cannot be generic", tok=nm)
        opaque = False
        fields = []
        if extern and self.is_op(";"):
            self.advance()
            opaque = True
        else:
            self.expect_op("{", "after struct name")
            while not self.is_op("}"):
                fn = self.expect_ident("field name")
                self.expect_op(":", "after field name")
                ty = self.parse_type()
                fields.append(N("Field", fn.span, name=fn.val, tyn=ty))
                if not self.accept_op(","):
                    break
            self.expect_op("}", "to close struct")
        return N("Struct", nm.span, name=nm.val, fields=fields, pub=pub, tparams=tparams, tbounds=tbounds,
                 extern=extern, opaque=opaque, cname_str=cname)

    def parse_enum(self, pub):
        self.advance()
        nm = self.expect_ident("enum name")
        self.expect_op("{", "after enum name")
        vs = []
        while not self.is_op("}"):
            v = self.expect_ident("variant name")
            vs.append((v.val, v.span))
            if not self.accept_op(","):
                break
        self.expect_op("}", "to close enum")
        return N("Enum", nm.span, name=nm.val, variants=vs, pub=pub)

    def parse_const(self, pub, extern=False, cname=None):
        self.advance()
        nm = self.expect_ident("constant name")
        if not self.is_op(":"):
            self.fail("constants require a type annotation", label="expected `: Type`",
                      help=f"write `const {nm.val}: i32 = ...;`")
        self.advance()
        ty = self.parse_type()
        if extern:
            self.expect_op(";", "after extern constant")
            return N("Const", nm.span, name=nm.val, tyn=ty, init=None, pub=pub, extern=True, cname_str=cname)
        self.expect_op("=", "in constant declaration")
        init = self.parse_expr()
        self.expect_op(";", "after constant")
        return N("Const", nm.span, name=nm.val, tyn=ty, init=init, pub=pub, extern=False, cname_str=None)

    def parse_error_decl(self, pub):
        self.advance()
        names = [self.expect_ident("error name")]
        while self.accept_op(","):
            names.append(self.expect_ident("error name"))
        self.expect_op(";", "after error declaration")
        return N("ErrorDecl", names[0].span, names=[(n.val, n.span) for n in names], pub=pub)

    # -------------------------------------------------------------- types
    def parse_ret_type(self):
        t = self.parse_type()
        if self.is_op("!"):
            bang = self.advance()
            return N("TErrUnion", t.span.join(bang.span), inner=t)
        return t

    def parse_type(self):
        t = self.tok
        ty = self.parse_type_base()
        while self.is_op("?") or self.is_op("??"):                 # `T?`: a value or nothing
            q = self.advance()
            if ty.kind == "TOpt" or q.val == "??":
                self.fail("optional types cannot be nested", tok=q, help="`T??` is the same as `T?`")
            ty = N("TOpt", t.span.join(q.span), inner=ty)
        return ty

    def parse_type_base(self):
        t = self.tok
        if t.kind == "op" and t.val == "*":
            self.advance()
            inner = self.parse_type()
            return N("TPtr", self.sp(t), inner=inner)
        if t.kind == "kw" and t.val == "fn":
            self.advance()
            self.expect_op("(", "in function type")
            ps = []
            while not self.is_op(")"):
                ps.append(self.parse_type())
                if not self.accept_op(","):
                    break
            self.expect_op(")", "after the parameter types of a function type")
            ret = self.parse_ret_type() if self.accept_op("->") else None
            return N("TFn", self.sp(t), params=ps, ret=ret)
        if t.kind == "op" and t.val in ("|", "||"):                  # closure type: `|A, B| -> R`
            self.advance()
            ps = []
            if t.val == "|":
                while not self.is_op("|"):
                    ps.append(self.parse_type())
                    if not self.accept_op(","):
                        break
                self.expect_op("|", "after the parameter types of a closure type")
            ret = self.parse_ret_type() if self.accept_op("->") else None
            return N("TClo", self.sp(t), params=ps, ret=ret)
        if t.kind == "op" and t.val == "[":
            self.advance()
            if self.accept_op("]"):
                inner = self.parse_type()
                return N("TSlice", self.sp(t), inner=inner)
            size = self.parse_expr()
            self.expect_op("]", "in array type")
            inner = self.parse_type()
            return N("TArray", self.sp(t), size=size, inner=inner)
        if t.kind == "ident" or (t.kind == "kw" and t.val == "error"):
            self.advance()
            path = [t.val]
            while self.is_op(".") and self.peek().kind == "ident":
                self.advance()
                path.append(self.advance().val)
            args = []
            if self.is_op("<") and path[-1] not in PRIMS and path[-1] != "error":
                self.advance()
                args = self.parse_type_args()
            if path == ["Self"] and not args and self.impl_ty is not None:
                return self.impl_ty
            return N("TName", self.sp(t), path=path, args=args)
        self.fail(f"expected a type, found {self.describe(t)}", label="expected a type")

    # --------------------------------------------------------- statements
    def parse_block(self):
        start = self.expect_op("{")
        saved = self.no_struct
        self.no_struct = False
        stmts = []
        while not self.is_op("}"):
            if self.tok.kind == "eof":
                self.d.error(start.span, "unclosed `{`", label="this block is never closed",
                             help="add a matching `}`")
                break
            s0 = self.p
            try:
                stmts.append(self.parse_stmt())
            except ParseAbort:
                self.sync_stmt()
                if self.p == s0:
                    self.advance()
        end = self.tok
        if self.is_op("}"):
            self.advance()
        self.no_struct = saved
        return N("Block", start.span.join(end.span), stmts=stmts, end_span=end.span)

    def parse_cond(self):
        saved = self.no_struct
        self.no_struct = True
        try:
            return self.parse_expr()
        finally:
            self.no_struct = saved

    def parse_stmt(self):
        t = self.tok
        if t.kind == "ident" and t.val == "region" and self.peek().kind == "ident" and self.peek(2).kind == "op" \
                and self.peek(2).val == "{":
            self.advance()
            nm = self.advance()
            body = self.parse_block()
            return N("Region", self.sp(t), name=nm.val, name_span=nm.span, body=body)
        if t.kind == "kw":
            v = t.val
            if v == "try" and self.peek().kind == "op" and self.peek().val == "{":
                self.advance()
                body = self.parse_block()
                if not self.is_kw("catch"):
                    self.fail("a `try { ... }` block needs a `catch` handler", tok=t,
                              label="expected `catch { ... }` or `catch |e| { ... }`")
                self.advance()
                binder = None
                if self.accept_op("|"):
                    binder = self.expect_ident("error binding")
                    self.expect_op("|", "after catch binding")
                handler = self.parse_block()
                return N("TryBlock", self.sp(t), body=body, binder=binder.val if binder else None,
                         binder_span=binder.span if binder else None, handler=handler)
            if v == "let":
                return self.parse_let()
            if v == "if":
                return self.parse_if()
            if v == "while":
                self.advance()
                cond = self.parse_cond()
                body = self.parse_block()
                return N("While", self.sp(t), cond=cond, body=body)
            if v == "loop":
                self.advance()
                body = self.parse_block()
                return N("Loop", self.sp(t), body=body)
            if v == "for":
                return self.parse_for()
            if v == "return":
                self.advance()
                e = None
                if not self.is_op(";"):
                    e = self.parse_expr()
                self.expect_op(";", "after `return`")
                return N("Return", self.sp(t), expr=e)
            if v == "break" or v == "continue":
                self.advance()
                self.expect_op(";", f"after `{v}`")
                return N("Break" if v == "break" else "Continue", self.sp(t))
            if v == "throw":
                self.advance()
                e = self.parse_expr()
                self.expect_op(";", "after `throw`")
                return N("Throw", self.sp(t), expr=e)
            if v == "match":
                return self.parse_match()
            if v in ("fn", "struct", "enum", "import", "pub", "extern", "export"):
                self.fail(f"`{v}` items are only allowed at the top level of a module",
                          label="declare it outside the function")
        if self.is_op("{"):
            return self.parse_block()
        # expression or assignment
        e = self.parse_expr()
        if self.tok.kind == "op" and self.tok.val in ASSIGN_OPS:
            op = self.advance()
            rhs = self.parse_expr()
            self.expect_op(";", "after assignment")
            return N("Assign", self.sp(t), target=e, op=op.val, value=rhs, op_span=op.span)
        self.expect_op(";", "after expression")
        return N("ExprStmt", self.sp(t), expr=e)

    def parse_let(self):
        t = self.advance()
        mut = bool(self.accept_kw("mut"))
        nm = self.expect_ident("variable name")
        tyn = None
        if self.accept_op(":"):
            tyn = self.parse_type()
            if self.is_op("!"):
                self.fail("error unions (`T!`) can only be function return types",
                          label="not allowed on a variable",
                          help="handle the error at the call site with `try` or `catch`")
        if not self.is_op("="):
            self.fail(f"variables must be initialized: expected `=`, found {self.describe(self.tok)}",
                      label="expected `= value`", help=f"write `let {nm.val} = <value>;`")
        self.advance()
        init = self.parse_expr()
        self.expect_op(";", "after `let` statement")
        return N("Let", self.sp(t), name=nm.val, mut=mut, tyn=tyn, init=init, name_span=nm.span)

    def parse_if(self):
        t = self.advance()
        cond = self.parse_cond()
        then = self.parse_block()
        els = None
        if self.accept_kw("else"):
            if self.is_kw("if"):
                els = self.parse_if()
            else:
                els = self.parse_block()
        return N("If", self.sp(t), cond=cond, then=then, els=els)

    def parse_for(self):
        t = self.advance()
        nm = self.expect_ident("loop variable")
        self.expect_kw("in")
        saved = self.no_struct
        self.no_struct = True
        try:
            lo = self.parse_expr()
            if self.is_op("..") or self.is_op("..="):
                inclusive = self.advance().val == "..="
                hi = self.parse_expr()
                it = N("Range", lo.span.join(hi.span), lo=lo, hi=hi, inclusive=inclusive)
            else:
                it = lo
        finally:
            self.no_struct = saved
        body = self.parse_block()
        return N("For", self.sp(t), var=nm.val, var_span=nm.span, iter=it, body=body)

    def parse_match(self):
        t = self.advance()
        scrut = self.parse_cond()
        self.expect_op("{", "after match scrutinee")
        arms = []
        while not self.is_op("}"):
            if self.tok.kind == "eof":
                self.fail("unclosed `match`")
            pats = []
            wild = False
            while True:
                if self.tok.kind == "ident" and self.tok.val == "_":
                    self.advance()
                    wild = True
                else:
                    saved = self.no_struct
                    self.no_struct = True
                    pats.append(self.parse_bin(6))  # tighter than `|`, which separates alternatives
                    self.no_struct = saved
                if not self.accept_op("|"):
                    break
            self.expect_op("=>", "after match pattern")
            body = self.parse_block()
            arms.append(N("Arm", body.span, pats=pats, wild=wild, body=body))
            self.accept_op(",")
        self.expect_op("}", "to close match")
        return N("Match", self.sp(t), scrut=scrut, arms=arms)

    # -------------------------------------------------------- expressions
    def parse_expr(self):
        start = self.tok
        lhs = self.parse_bin(1)
        if self.is_op("?"):                                        # cond ? a : b
            self.advance()
            saved = self.no_struct
            self.no_struct = False
            a = self.parse_expr()
            self.no_struct = saved
            self.expect_op(":", "in the `cond ? a : b` expression")
            b = self.parse_expr()
            lhs = N("Ternary", self.sp(start), cond=lhs, a=a, b=b)
        while self.is_kw("catch"):
            self.advance()
            binder = None
            if self.accept_op("|"):
                b = self.expect_ident("error binding")
                binder = b
                self.expect_op("|", "after catch binding")
            if self.is_op("{"):
                saved = self.no_struct
                handler = self.parse_block()
                self.no_struct = saved
            else:
                if binder is not None:
                    self.fail("expected `{` after `catch |name|`")
                handler = self.parse_bin(1)
            lhs = N("Catch", self.sp(start), expr=lhs, binder=binder.val if binder else None,
                    binder_span=binder.span if binder else None, handler=handler)
        return lhs

    def parse_bin(self, minprec):
        start = self.tok
        lhs = self.parse_cast()
        while True:
            t = self.tok
            if t.kind != "op" or t.val not in BIN_PREC:
                break
            prec = BIN_PREC[t.val]
            if prec < minprec:
                break
            self.advance()
            rhs = self.parse_bin(prec + 1)
            lhs = N("Binary", self.sp(start), op=t.val, l=lhs, r=rhs, op_span=t.span)
            if t.val in CMP_OPS and self.tok.kind == "op" and self.tok.val in CMP_OPS:
                self.fail("comparison operators cannot be chained",
                          help="use `&&` to combine comparisons, e.g. `a < b && b < c`")
        return lhs

    def parse_cast(self):
        start = self.tok
        e = self.parse_unary()
        while self.is_kw("as"):
            self.advance()
            ty = self.parse_type()
            e = N("Cast", self.sp(start), expr=e, tyn=ty)
        return e

    def parse_unary(self):
        t = self.tok
        if t.kind == "op" and t.val in ("++", "--"):
            self.advance()
            operand = self.parse_unary()
            return N("IncDec", self.sp(t), op=t.val, prefix=True, expr=operand)
        if t.kind == "op" and t.val in ("-", "!", "~", "*", "&"):
            self.advance()
            operand = self.parse_unary()
            return N("Unary", self.sp(t), op=t.val, expr=operand)
        if (t.kind == "ident" and t.val == "await" and (self.peek().kind in ("ident", "int", "float", "str", "char")
                or self.peek().kind == "kw" and self.peek().val in ("true", "false", "try")
                or self.is_op("(", 1) or self.is_op("*", 1) or self.is_op("&", 1))):
            self.advance()                                           # `await task` is `task.wait()`
            operand = self.parse_unary()
            return N("Call", self.sp(t), callee=N("Field", self.sp(t), obj=operand, name="wait", name_span=t.span),
                     args=[], targ=None, targs=None)
        if t.kind == "kw" and t.val == "try":
            self.advance()
            operand = self.parse_unary()
            return N("Try", self.sp(t), expr=operand)
        return self.parse_postfix()

    def parse_args(self):
        saved = self.no_struct
        self.no_struct = False
        args = []
        while not self.is_op(")"):
            if self.tok.kind == "ident" and self.is_op("=", 1):      # `name = value`
                nm = self.advance()
                self.advance()
                val = self.parse_expr()
                args.append(N("NamedArg", nm.span.join(val.span), name=nm.val, name_span=nm.span, expr=val))
            else:
                args.append(self.parse_expr())
            if not self.accept_op(","):
                break
        self.no_struct = saved
        self.expect_op(")", "after arguments")
        return args

    def is_path(self, e):
        return e.kind == "Ident" or (e.kind == "Field" and self.is_path(e.obj))

    def parse_postfix(self):
        start = self.tok
        e = self.parse_primary()
        while True:
            t = self.tok
            if t.kind != "op":
                break
            if t.val == "(":
                self.advance()
                targ = None
                if e.kind == "Ident" and e.name in TYPE_ONLY_BUILTINS:
                    saved = self.no_struct
                    self.no_struct = False
                    targ = self.parse_type()
                    self.no_struct = saved
                    self.expect_op(")", f"after the type in `{e.name}(T)`")
                    e = N("Call", self.sp(start), callee=e, args=[], targ=targ, targs=None)
                    continue
                if e.kind == "Ident" and e.name in TYPE_ARG_BUILTINS:
                    saved = self.no_struct
                    self.no_struct = False
                    targ = self.parse_type()
                    self.no_struct = saved
                    if not self.accept_op(","):
                        self.fail(f"`{e.name}` expects a type and a count: `{e.name}(T, n)`")
                args = self.parse_args()
                e = N("Call", self.sp(start), callee=e, args=args, targ=targ, targs=None)
            elif t.val == "[":
                self.advance()
                saved = self.no_struct
                self.no_struct = False
                e = self.parse_index_tail(start, e)
                self.no_struct = saved
            elif t.val == "?." :                      # optional chain: `a?.b`, `a?.f(x)`
                self.advance()
                nm = self.expect_ident("field or method name")
                args = None
                if self.is_op("("):
                    self.advance()
                    args = self.parse_args()
                e = N("OptChain", self.sp(start), obj=e, name=nm.val, name_span=nm.span, args=args)
            elif t.val == "?" and self.is_op("??", 1):  # `name? ?? "x"`: the `?` just marks the chain as optional
                self.advance()
            elif t.val == "." and self.is_kw("catch", 1) and self.is_op("(", 2):     # expr.catch(|e| handler)
                self.advance()
                self.advance()
                self.advance()
                binder = None
                if self.accept_op("|"):
                    binder = self.expect_ident("error binding")
                    self.expect_op("|", "after the error name")
                saved = self.no_struct
                self.no_struct = False
                if self.is_op("{"):
                    handler = self.parse_block()
                else:
                    handler = self.parse_expr()
                self.no_struct = saved
                self.expect_op(")", "after the catch handler")
                e = N("Catch", self.sp(start), expr=e, binder=binder.val if binder else None,
                      binder_span=binder.span if binder else None, handler=handler, lambda_form=True)
            elif t.val == ".":
                self.advance()
                nm = self.expect_ident("field name")
                e = N("Field", self.sp(start), obj=e, name=nm.val, name_span=nm.span)
            elif t.val == "->":                       # `p->x` is `(*p).x`, `p->f(..)` is `(*p).f(..)`
                self.advance()
                nm = self.expect_ident("field or method name")
                e = N("Field", self.sp(start), obj=N("Unary", self.sp(start), op="*", expr=e), name=nm.val, name_span=nm.span)
            elif t.val in ("++", "--"):
                self.advance()
                e = N("IncDec", self.sp(start), op=t.val, prefix=False, expr=e)
            elif t.val == "<" and self.is_path(e) and e.kind in ("Ident", "Field"):
                targs = self.try_explicit_targs()
                if targs is None:
                    break
                self.advance()  # the `(`
                args = self.parse_args()
                e = N("Call", self.sp(start), callee=e, args=args, targ=None, targs=targs)
            elif t.val == "{" and self.is_path(e) and self.looks_like_struct_lit():
                e = self.parse_struct_lit(start, e)
            else:
                break
        return e

    def try_explicit_targs(self):
        """At `<` after a path: speculatively parse `<T, U>` followed by `(`. Returns None (and rewinds) otherwise."""
        save_p, save_n = self.p, len(self.d.items)
        self.undo = []
        try:
            self.advance()
            targs = self.parse_type_args()
            if self.is_op("("):
                self.undo = None
                return targs
        except ParseAbort:
            pass
        for idx, tok in reversed(self.undo):
            self.toks[idx] = tok
        self.undo = None
        self.p = save_p
        del self.d.items[save_n:]
        return None

    def looks_like_struct_lit(self):
        # at `{`
        a, b = self.peek(1), self.peek(2)
        if a.kind == "ident" and b.kind == "op" and b.val == ":":
            return True
        if a.kind == "op" and a.val == "}" and not self.no_struct:
            return True
        return False

    def parse_struct_lit(self, start, path):
        self.expect_op("{")
        saved = self.no_struct
        self.no_struct = False
        fields = []
        while not self.is_op("}"):
            nm = self.expect_ident("field name")
            self.expect_op(":", "after field name")
            val = self.parse_expr()
            fields.append((nm.val, nm.span, val))
            if not self.accept_op(","):
                break
        self.no_struct = saved
        self.expect_op("}", "to close struct literal")
        return N("StructLit", self.sp(start), path=path, fields=fields)

    def parse_index_tail(self, start, base):
        # after '['
        lo = hi = None
        if self.is_op(".."):
            self.advance()
            if not self.is_op("]"):
                hi = self.parse_expr()
            self.expect_op("]", "after slice range")
            return N("SliceExpr", self.sp(start), obj=base, lo=None, hi=hi)
        idx = self.parse_expr()
        if self.is_op(".."):
            self.advance()
            if not self.is_op("]"):
                hi = self.parse_expr()
            self.expect_op("]", "after slice range")
            return N("SliceExpr", self.sp(start), obj=base, lo=idx, hi=hi)
        self.expect_op("]", "after index")
        return N("Index", self.sp(start), obj=base, idx=idx)

    def parse_closure(self):
        """`[move] |a, b: T| [-> R] expr-or-block`"""
        start = self.tok
        is_move = False
        if start.kind == "ident" and start.val == "move":
            self.advance()
            is_move = True
        bar = self.tok
        params = []
        if bar.val == "||":
            self.advance()
        else:
            self.expect_op("|")
            while not self.is_op("|"):
                nm = self.expect_ident("closure parameter name")
                ty = self.parse_type() if self.accept_op(":") else None
                params.append(N("Param", nm.span, name=nm.val, tyn=ty, name_span=nm.span, mut=False))
                if not self.accept_op(","):
                    break
            self.expect_op("|", "after the closure parameters")
        ret = self.parse_ret_type() if self.accept_op("->") else None
        if self.is_op("{"):
            body = self.parse_block()
            expr_body = False
        else:
            saved = self.no_struct
            e = self.parse_expr()
            self.no_struct = saved
            body = e
            expr_body = True
        return N("Closure", self.sp(start), params=params, ret=ret, body=body, expr_body=expr_body, move=is_move)

    def parse_primary(self):
        t = self.tok
        k = t.kind
        if (k == "op" and t.val in ("|", "||")) or (k == "ident" and t.val == "move" and self.peek().kind == "op"
                                                       and self.peek().val in ("|", "||")):
            return self.parse_closure()
        if k == "int":
            self.advance()
            return N("Int", t.span, value=t.val, suffix=t.suffix)
        if k == "char":
            self.advance()
            return N("Int", t.span, value=t.val, suffix="u8")
        if k == "float":
            self.advance()
            return N("Float", t.span, value=t.val, suffix=t.suffix)
        if k == "str":
            self.advance()
            return N("Str", t.span, value=t.val)
        if k == "kw" and t.val in ("true", "false"):
            self.advance()
            return N("Bool", t.span, value=(t.val == "true"))
        if k == "ident":
            self.advance()
            if t.val == "Self" and self.impl_ty is not None:      # `Self.new(..)` inside an impl block
                node = N("Ident", t.span, name=self.impl_ty.path[0])
                for part in self.impl_ty.path[1:]:
                    node = N("Field", t.span, obj=node, name=part, name_span=t.span)
                return node
            return N("Ident", t.span, name=t.val)
        if k == "op" and t.val == "(":
            self.advance()
            saved = self.no_struct
            self.no_struct = False
            e = self.parse_expr()
            self.no_struct = saved
            self.expect_op(")", "to close parenthesis")
            e.parenthesized = True
            return e
        if k == "op" and t.val == "[":
            return self.parse_array_lit()
        if k == "kw" and t.val == "error":
            self.fail("`error` is a keyword; declare errors with `error Name;` and refer to them by name",
                      label="not an expression")
        self.fail(f"expected an expression, found {self.describe(t)}", label="expected an expression")

    def parse_array_lit(self):
        start = self.advance()
        saved = self.no_struct
        self.no_struct = False
        try:
            if self.is_op("]"):
                self.advance()
                return N("ArrayLit", self.sp(start), elems=[])
            first = self.parse_expr()
            if self.accept_op(";"):
                count = self.parse_expr()
                self.expect_op("]", "after array repeat")
                return N("ArrayRepeat", self.sp(start), elem=first, count=count)
            elems = [first]
            while self.accept_op(","):
                if self.is_op("]"):
                    break
                elems.append(self.parse_expr())
            self.expect_op("]", "to close array literal")
            return N("ArrayLit", self.sp(start), elems=elems)
        finally:
            self.no_struct = saved


def parse(src, diags):
    p = Parser(src, diags)
    t0 = time.perf_counter()
    with TIMINGS.phase("parse"):
        ast = p.parse_module()
    if TIMINGS.enabled:
        TIMINGS.files.append((src.path, src.text.count("\n") + 1, len(p.toks), p.lex_time, time.perf_counter() - t0))
        TIMINGS.add("tokens", len(p.toks))
        TIMINGS.add("lines", src.text.count("\n") + 1)
    return ast
