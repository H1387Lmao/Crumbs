"""Recursive-descent parser with panic-mode error recovery."""
from .diag import Span
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
    "||": 2, "&&": 3,
    "==": 4, "!=": 4, "<": 4, ">": 4, "<=": 4, ">=": 4,
    "|": 5, "^": 6, "&": 7, "<<": 8, ">>": 8,
    "+": 9, "-": 9,
    "*": 10, "/": 10, "%": 10, "*%": 10,
}
CMP_OPS = {"==", "!=", "<", ">", "<=", ">="}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
ITEM_START = {"fn", "struct", "enum", "const", "import", "pub", "extern", "error", "export"}
TYPE_ARG_BUILTINS = {"alloc"}
TYPE_ONLY_BUILTINS = {"size_of"}
DIRECTIVES = {"link", "include", "define"}


class Parser:
    def __init__(self, src, diags):
        self.src = src
        self.d = diags
        self.toks = lex(src, diags)
        self.p = 0
        self.no_struct = False
        self.undo = None
        self.impl_ty = None          # type node of the `impl` block being parsed (for `self` / `Self`)

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
        names = []
        if self.accept_op("<"):
            while True:
                t = self.expect_ident("type parameter name")
                if t.val in names:
                    self.fail(f"type parameter `{t.val}` is declared more than once", tok=t)
                names.append(t.val)
                if not self.accept_op(","):
                    break
            self.expect_gt()
        return names

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
                if it:
                    items.append(it)
            except ParseAbort:
                self.sync_item()
                if self.p == start:
                    self.advance()
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
        return N("Impl", self.sp(start), tparams=tparams, tyn=ty, fns=fns)

    def parse_item(self):
        start = self.tok
        pub = bool(self.accept_kw("pub"))
        t = self.tok
        if not pub and self.is_directive():
            return self.parse_directive()
        if t.kind == "ident" and t.val == "drop" and (self.peek().kind == "ident" or self.peek().kind == "op" and self.peek().val == "<"):
            return self.parse_drop(pub)
        if t.kind == "ident" and t.val == "impl" and (self.peek().kind == "ident" or self.peek().kind == "op" and self.peek().val == "<"):
            if pub:
                self.fail("`pub` is not allowed on `impl` blocks; mark the functions inside as `pub`", tok=start)
            return self.parse_impl()
        if t.kind != "kw":
            self.fail(f"expected an item (`fn`, `struct`, `enum`, `const`, `error`, `import`, `impl`), found {self.describe(t)}",
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
            params.append(N("Param", nm.span.join(self.prev.span), name=nm.val, tyn=ty, name_span=nm.span, mut=is_mut))
            if not self.accept_op(","):
                break
        self.expect_op(")", "after parameters")
        return params, variadic

    def parse_fn(self, pub, extern, cname=None, export=False):
        start = self.expect_kw("fn")
        nm = self.expect_ident("function name")
        tparams = self.parse_tparams()
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
                 tparams=tparams, full_span=self.sp(start), export=export, cname_str=cname, variadic=variadic,
                 drop_for=None)

    def parse_struct(self, pub, extern=False, cname=None):
        self.advance()
        nm = self.expect_ident("struct name")
        tparams = self.parse_tparams()
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
        return N("Struct", nm.span, name=nm.val, fields=fields, pub=pub, tparams=tparams, extern=extern,
                 opaque=opaque, cname_str=cname)

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
        if t.kind == "kw":
            v = t.val
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
        lhs = self.parse_bin(2)
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
                handler = self.parse_bin(2)
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
            elif t.val == ".":
                self.advance()
                nm = self.expect_ident("field name")
                e = N("Field", self.sp(start), obj=e, name=nm.val, name_span=nm.span)
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

    def parse_primary(self):
        t = self.tok
        k = t.kind
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
    return Parser(src, diags).parse_module()
