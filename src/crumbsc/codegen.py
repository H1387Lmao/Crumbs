"""C code generation from the type-annotated AST."""
from .runtime import RUNTIME
from .timing import TIMINGS
from .types import INTS, STR, STRING, BOOL, VOID, ERR, opt as opt_ty

C_RESERVED = set("""
auto break case char const continue default do double else enum extern float for goto if inline int long
register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while
_Bool _Complex _Imaginary bool true false NULL main asm typeof
printf puts putchar getchar fprintf fputs fwrite fread fflush fopen fclose snprintf sprintf scanf exit abort atexit
free malloc calloc realloc memcpy memcmp memset memmove strlen strcmp strcpy strncmp strchr strstr strtod strtof strtol
stdout stderr stdin errno abs labs llabs fabs fabsf sqrt sqrtf pow sin cos tan asin acos atan atan2 exp log log2 log10
floor ceil round trunc fmod isinf isnan time clock rand srand atoi atof qsort getenv system signal
int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t size_t ptrdiff_t intptr_t uintptr_t
NAN INFINITY EOF INT_MAX INT_MIN SIZE_MAX M_PI index rindex y0 y1 j0 j1 gamma
""".split())

PRIM_C = {
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "f32": "float", "f64": "double", "bool": "bool", "str": "cb_str", "String": "cb_String", "void": "void",
    "Region": "cb_Region *",
}
OPNAME = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "rem"}


def c_string(b):
    out = []
    for ch in b:
        if ch == 34:
            out.append('\\"')
        elif ch == 92:
            out.append("\\\\")
        elif ch == 63:
            out.append("\\?")
        elif 32 <= ch < 127:
            out.append(chr(ch))
        else:
            out.append("\\%03o" % ch)
    return '"' + "".join(out) + '"'


class Tmp:
    """A compiler temporary that holds a value with a destructor (behaves like a glue local)."""

    def __init__(self, cname, ty):
        self.cname = cname
        self.ty = ty


class RegionLocal:
    """The scope entry that frees a `region` on every way out of its block."""
    region = True

    def __init__(self, cname):
        self.cname = cname
        self.ty = None


class CodeGen:
    def __init__(self, ck, gc=False, nostd=False, overflow_panic=False, gc_mode="auto", gc_interval=100):
        self.ck = ck
        self.gc = gc and not nostd
        self.gc_mode = gc_mode if self.gc else "auto"
        self.gc_interval = gc_interval
        self.polls = self.gc_mode == "thread"      # safepoint polls: only the collector thread needs them
        self.nostd = nostd
        self.overflow_panic = overflow_panic
        self.out = []
        self.ind = 1
        self.n = 0
        self.reg = {}
        self.arrs = []
        self.dbg_done = {}
        self.dbg_todo = []
        self.slices = []
        self.eus = []
        self.opts = []
        self.fnts = []
        self.cur_ret = VOID
        self.dscopes = []        # per C scope: locals whose type has a destructor
        self.loop_bases = []
        self.try_stack = []
        self.glue_done = {}
        self.glue_todo = []
        self.stmt_temps = None   # temporaries with destructors created while generating the current statement

    # ------------------------------------------------------------ utilities
    def tmp(self, base="cb_t"):
        self.n += 1
        return f"{base}{self.n}"

    def emit(self, line):
        self.out.append("    " * self.ind + line)

    def poll(self):
        """A safepoint for the background collector thread (--gc-mode=thread): park here if a collection is waiting."""
        if self.polls:
            self.emit("cb_gc_poll();")

    def capture(self, fn):
        saved, self.out = self.out, []
        fn()
        text = "\n".join(self.out)
        self.out = saved
        return text

    def loc(self, span):
        return c_string(f"{span.src.path}:{span.line}:{span.col}".encode())

    def fld(self, name):
        return name + "_" if (name in C_RESERVED or name.startswith("cb_")) else name

    # ---------------------------------------------------------------- types
    def ctype(self, t):
        k = t.kind
        if k == "prim":
            return PRIM_C[t.name]
        if k == "err":
            return "int32_t"
        if k in ("struct", "enum"):
            return t.sym.cname
        if k == "ptr":
            return self.ctype(t.inner) + "*"
        if k == "fn":
            if t.key in self.reg:
                return self.reg[t.key]
            ps = [self.ctype(p) for p in t.params]
            ret = self.ctype(t.inner) if t.inner != VOID else "void"
            name = f"cb_fn_{len(self.reg) + 1}"
            self.reg[t.key] = name
            self.fnts.append(f"typedef {ret} (*{name})({', '.join(ps) or 'void'});")
            return name
        if k == "opt":
            if t.key in self.reg:
                return self.reg[t.key]
            self.ctype(t.inner)
            name = f"cb_opt_{len(self.reg) + 1}"
            self.reg[t.key] = name
            self.opts.append((name, t))
            return name
        if k in ("arr", "slice", "eu"):
            if t.key in self.reg:
                return self.reg[t.key]
            inner = self.ctype(t.inner) if not (k == "eu" and t.inner == VOID) else None
            prefix = {"arr": "cb_arr", "slice": "cb_slice", "eu": "cb_eu"}[k]
            name = f"{prefix}_{len(self.reg) + 1}"
            self.reg[t.key] = name
            {"arr": self.arrs, "slice": self.slices, "eu": self.eus}[k].append((name, t, inner))
            return name
        raise AssertionError(f"no C type for {t}")

    def int_lit(self, v, t):
        if t.name == "i64":
            if v == -(1 << 63):
                return "(-INT64_C(9223372036854775807)-1)"
            return f"INT64_C({v})"
        if t.name == "u64":
            return f"UINT64_C({v})"
        ct = PRIM_C[t.name]
        lo = t.int_range()[0]
        if v < 0 and v == lo:
            return f"(({ct})({v + 1}-1))"
        return f"(({ct}){v})"

    def float_lit(self, v, t):
        r = repr(float(v))
        if "." not in r and "e" not in r and "n" not in r:
            r += ".0"
        return r + ("f" if t.name == "f32" else "")

    def is_simple(self, e):
        """Side-effect free and cheap to evaluate twice."""
        k = e.kind
        if k == "Ident":
            return True
        if k == "Int":
            return True
        if k == "Field":
            return getattr(e, "mode", None) in ("field", "ptrfield", "sym", "variant") and (
                e.mode in ("sym", "variant") or self.is_simple(e.obj))
        if k == "Unary" and e.op == "*":
            return self.is_simple(e.expr)
        if k == "Index":
            return self.is_simple(e.obj) and self.is_simple(e.idx)
        return False

    def is_place(self, e):
        k = e.kind
        if k == "Ident":
            return getattr(e, "local", None) is not None
        if k == "Field":
            return getattr(e, "mode", None) in ("field", "ptrfield")
        return k == "Index" or (k == "Unary" and e.op == "*")

    @staticmethod
    def strip(x):
        """Remove one redundant pair of outer parentheses."""
        if not (x.startswith("(") and x.endswith(")")):
            return x
        depth, i, in_str = 0, 0, False
        while i < len(x):
            c = x[i]
            if in_str:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0 and i != len(x) - 1:
                    return x
            i += 1
        return x[1:-1]

    # ---------------------------------------------------------- expressions
    def ex(self, e):
        c = getattr(self, "g_" + e.kind)(e)
        conv = getattr(e, "conv", None)
        if conv is not None:                  # implicit numeric conversion
            src, dst, kind = conv
            if kind == "checked":
                c = f"cb_cvt_{dst.name}((__int128)({c}), {self.loc(e.span)})"
            else:
                c = f"(({self.ctype(dst)})({c}))"
        if (self.stmt_temps is not None and e.kind in ("Call", "StructLit", "ArrayLit", "Binary", "Try", "Catch", "Ternary")
                and not getattr(e, "owned", False) and e.ty is not None and not e.ty.poison and e.ty.kind != "eu"
                and self.ck.has_glue(e.ty)):
            # a temporary that is only borrowed (printed, viewed, compared, ...): destroy it after the statement
            name = self.tmp("cb_tmp")
            self.stmt_temps.append(Tmp(name, e.ty))
            if self.dscopes:
                self.dscopes[-1].append(self.stmt_temps[-1])
            c = f"({name} = {c}, {name}__live = 1, {name})"
        if getattr(e, "auto_try", False):     # a failing call inside `try { }`
            c = self.try_wrap(c, e.eu_ty)
        if getattr(e, "coerce", False):       # String -> str view
            c = f"cb_string_view({c})"
        wrap = getattr(e, "wrap_opt", None)
        if wrap is not None:                  # a plain value where an optional is expected
            c = f"(({self.ctype(wrap)}){{1, {c}}})"
        return c

    def sv(self, e):
        """C expression for `e` viewed as a cb_str (works for both str and String)."""
        c = self.ex(e)
        return f"cb_string_view({c})" if e.ty == STRING and not getattr(e, "coerce", False) else c

    def nn(self, x, ptr_ty, span):
        """Null-checked pointer expression."""
        return f"(({self.ctype(ptr_ty)})cb_nn((void *)({x}), {self.loc(span)}))"

    def g_Int(self, e):
        return self.int_lit(e.value, e.ty)

    def g_Float(self, e):
        return self.float_lit(e.value, e.ty)

    def g_Bool(self, e):
        return "true" if e.value else "false"

    # ------------------------------------------------------------ interpolation
    def dbg_fn(self, t):
        """Name of the C function writing the automatic `{:?}` representation of a `t` into a cb_fb."""
        got = self.dbg_done.get(t.key)
        if got:
            return got
        name = f"cb_dbg_{len(self.dbg_done) + 1}"
        self.dbg_done[t.key] = name
        self.dbg_todo.append((name, t))
        return name

    def gen_dbg(self, name, t):
        ct = self.ctype(t)
        L = [f"static void {name}(cb_fb *b, const {ct} *p) {{"]
        if t.is_int:
            L.append("    cb_fb_i(b, (long long)*p);" if t.signed else "    cb_fb_u(b, (unsigned long long)*p);")
        elif t.is_float:
            L.append(f"    cb_fb_f(b, (double)*p, {1 if t.name == 'f32' else 0});")
        elif t == BOOL:
            L.append('    cb_fb_cstr(b, *p ? "true" : "false");')
        elif t == STR:
            L.append("    cb_fb_q(b, *p);")
        elif t == STRING:
            L.append("    cb_fb_q(b, cb_string_view(*p));")
        elif t == ERR:
            L.append("    cb_fb_cstr(b, cb_err_names[*p]);")
        elif t.kind == "enum":
            L.append(f"    cb_fb_cstr(b, {t.sym.cname}__names[*p]);")
        elif t.kind in ("ptr", "fn"):
            L.append("    cb_fb_ptr(b, (const void *)*p);")
        elif t.kind == "opt":
            L.append(f'    if (p->has) {self.dbg_fn(t.inner)}(b, &p->v); else cb_fb_cstr(b, "null");')
        elif t.kind in ("arr", "slice"):
            inner = self.dbg_fn(t.inner)
            n = str(t.n) if t.kind == "arr" else "p->len"
            el = "p->v[i]" if t.kind == "arr" else "p->ptr[i]"
            L.append('    cb_fb_cstr(b, "[");')
            L.append(f"    for (int64_t i = 0; i < {n}; i++) {{")
            L.append('        if (i) cb_fb_cstr(b, ", ");')
            L.append(f"        {inner}(b, &{el});")
            L.append("    }")
            L.append('    cb_fb_cstr(b, "]");')
        elif t.kind == "struct":
            L.append(f"    cb_fb_cstr(b, {c_string(t.sym.name.encode())});")
            fields = list(t.sym.fields)
            if fields:
                L.append('    cb_fb_cstr(b, " { ");')
                for i, (fname, ft, _) in enumerate(fields):
                    sep = ", " if i else ""
                    L.append(f'    cb_fb_cstr(b, "{sep}{fname}: ");')
                    L.append(f"    {self.dbg_fn(ft)}(b, &p->{self.fld(fname)});")
                L.append('    cb_fb_cstr(b, " }");')
        else:
            raise AssertionError(t)
        L.append("}")
        return "\n".join(L)

    def spec_lit(self, sp, type_override=None):
        def ch(c):
            return str(ord(c)) if c else "0"
        ty = sp.type if type_override is None else type_override
        grp = {"": 0, ",": 1, "_": 2}[sp.group]
        return (f"(&(const cb_fspec){{{ch(sp.fill)}, {ch(sp.align)}, {ch(sp.sign)}, {ch(ty)}, "
                f"{int(sp.alt)}, {int(sp.zero)}, {grp}, {sp.width}, {sp.prec}}})")

    def g_FStr(self, e):
        from .fmtspec import Spec
        b = self.tmp("cb_fb")
        out = [f"cb_fb {b}; cb_fb_init(&{b});"]
        for part in e.parts:
            if part.lit is not None:
                if part.lit:
                    out.append(f"cb_fb_put(&{b}, {c_string(part.lit)}, {len(part.lit)});")
                continue
            sp = getattr(part, "pspec", None) or Spec()
            t = part.expr.ty
            x = self.ex(part.expr)
            mode = part.mode
            lit = self.spec_lit(sp)
            if mode == "int":
                out.append(f"cb_fmt_int(&{b}, (long long)({x}), {1 if t.signed else 0}, {lit});")
            elif mode == "float":
                out.append(f"cb_fmt_float(&{b}, (double)({x}), {1 if t.name == 'f32' else 0}, {lit});")
            elif mode == "bool":
                out.append(f'cb_fmt_str(&{b}, cb_str_view_c(({x}) ? "true" : "false"), {lit});')
            elif mode == "text":
                if t == STR:
                    v = x
                elif t == STRING:
                    v = f"cb_string_view({x})"
                else:
                    v = f"cb_str_view_c(cb_err_names[{x}])"
                out.append(f"cb_fmt_str(&{b}, {v}, {lit});")
            elif mode == "enum":
                if sp.type == "d":
                    out.append(f"cb_fmt_int(&{b}, (long long)({x}), 1, {self.spec_lit(sp, '')});")
                else:
                    out.append(f"cb_fmt_str(&{b}, cb_str_view_c({t.sym.cname}__names[{x}]), {self.spec_lit(sp, 's')});")
            elif mode == "ptr":
                out.append(f"cb_fmt_ptr(&{b}, (const void *)({x}), {lit});")
            elif mode == "debug":
                v, tb = self.tmp(), self.tmp("cb_fb")
                out.append(f"{{ {self.ctype(t)} {v} = {x}; cb_fb {tb}; cb_fb_init(&{tb}); {self.dbg_fn(t)}(&{tb}, &{v}); "
                           f"cb_fmt_str(&{b}, (cb_str){{(const uint8_t *){tb}.p, (int64_t){tb}.len}}, {self.spec_lit(sp, 's')}); cb_fb_free(&{tb}); }}")
            else:
                raise AssertionError(mode)
        out.append(f"cb_fb_finish(&{b}, {self.loc(e.span)});")
        return "({ " + " ".join(out) + " })"

    def g_Str(self, e):
        return f"((cb_str){{(const uint8_t *){c_string(e.value)}, {len(e.value)}}})"

    def sym_ref(self, sym):
        if sym.kind == "const" and sym.extern:        # a C macro or constant: evaluate its expression in C
            return f"(({self.ctype(sym.ty)})({sym.csym}))"
        return sym.cname

    def g_Ident(self, e):
        if getattr(e, "is_null", False):
            if e.ty.kind == "opt":
                return f"(({self.ctype(e.ty)}){{0}})"
            return f"(({self.ctype(e.ty)})0)"
        loc = getattr(e, "local", None)
        if loc is not None:
            if getattr(e, "move", False) and loc.glue:
                return f"({loc.cname}__live = 0, {loc.cname})"
            return loc.cname
        return self.sym_ref(e.sym)

    def g_Field(self, e):
        m = e.mode
        if m == "sym":
            return self.sym_ref(e.sym)
        if m == "slice_ptr":
            ot = e.obj.ty
            x = f"{self.nn(self.ex(e.obj), ot, e.span)}->ptr" if ot.kind == "ptr" else f"({self.ex(e.obj)}).ptr"
            return f"((uint8_t *)({x}))" if (ot.inner if ot.kind == "ptr" else ot) == STR else f"({x})"
        if m == "variant":
            return f"{e.sym.cname}__{e.name}"
        if m == "field":
            return f"{self.ex(e.obj)}.{self.fld(e.name)}"
        if m == "ptrfield":
            return f"{self.nn(self.ex(e.obj), e.obj.ty, e.span)}->{self.fld(e.name)}"
        if m == "cap":
            ot = e.obj.ty
            if ot.kind == "ptr":
                return f"({self.nn(self.ex(e.obj), ot, e.span)}->cap)"
            return f"({self.ex(e.obj)}).cap"
        ot = e.obj.ty
        if m == "len_arr":
            base = ot.inner if ot.kind == "ptr" else ot
            if ot.kind != "ptr" and not self.is_simple(e.obj):
                return f"((void)({self.ex(e.obj)}), INT64_C({base.n}))"
            return f"INT64_C({base.n})"
        if m == "len_slice":
            if ot.kind == "ptr":
                return f"({self.nn(self.ex(e.obj), ot, e.span)}->len)"
            return f"({self.ex(e.obj)}).len"
        raise AssertionError(m)

    def g_Unary(self, e):
        op = e.op
        t = e.ty
        if op == "-":
            if e.expr.kind == "Int" and not e.expr.suffix and not getattr(e.expr, "parenthesized", False):
                return self.int_lit(-e.expr.value, t)
            x = self.ex(e.expr)
            if t.is_float:
                return f"(-{x})"
            return f"cb_neg_{t.name}({x}, {self.loc(e.span)})"
        if op == "!":
            return f"(!{self.ex(e.expr)})"
        if op == "~":
            return f"(({self.ctype(t)})~{self.ex(e.expr)})"
        if op == "*":
            return f"(*{self.nn(self.ex(e.expr), e.expr.ty, e.span)})"
        if op == "&":
            if getattr(e, "temp", False):       # a borrowed temporary: point at a copy that lives for the enclosing block
                return f"(&(({self.ctype(e.expr.ty)}[]){{{self.ex(e.expr)}}})[0])"
            return f"(&{self.ex(e.expr)})"
        raise AssertionError(op)

    def binop(self, op, t, l, r, span):
        """C expression for `l op r` where both operands have type `t`."""
        if op in ("&&", "||"):
            return f"({l} {op} {r})"
        if op in ("==", "!=", "<", ">", "<=", ">="):
            if t == STR:
                return f"(cb_str_eq({l}, {r}))" if op == "==" else f"(!cb_str_eq({l}, {r}))"
            return f"({l} {op} {r})"
        if op == "+" and t == STR:
            return f"cb_str_catv((cb_str[]){{{l}, {r}}}, 2, {self.loc(span)})"
        if op in OPNAME:
            if t.is_float:
                return f"({l} {op} {r})"
            return f"cb_{OPNAME[op]}_{t.name}({l}, {r}, {self.loc(span)})"
        if op in ("&", "|", "^"):
            return f"(({self.ctype(t)})({l} {op} {r}))"
        if op == "<<":
            return f"cb_shl_{t.name}({l}, (int64_t)({r}), {self.loc(span)})"
        if op == ">>":
            return f"cb_shr_{t.name}({l}, (int64_t)({r}), {self.loc(span)})"
        raise AssertionError(op)

    def g_Binary(self, e):
        if e.op == "??":
            return self.g_Coalesce_(e)
        if getattr(e, "opt_null", False):
            x = self.ex(e.l if e.opt_side == "l" else e.r)
            return f"(({x}).has)" if e.op == "!=" else f"(!({x}).has)"
        if e.op == "+" and getattr(e, "strk", None):
            parts = []

            def collect(n):
                if n.kind == "Binary" and n.op == "+" and getattr(n, "strk", None) and not getattr(n, "coerce", False):
                    collect(n.l)
                    collect(n.r)
                else:
                    parts.append(self.sv(n))
            collect(e.l)                       # (not `e` itself: it may carry `coerce` when a String is passed as a `str`)
            collect(e.r)
            fn = "cb_string_catv" if e.strk == "String" else "cb_str_catv"
            return f"{fn}((cb_str[]){{{', '.join(parts)}}}, {len(parts)}, {self.loc(e.op_span)})"
        if e.opty == STR and e.op in ("==", "!="):
            l, r = self.sv(e.l), self.sv(e.r)
            return self.binop(e.op, STR, l, r, e.op_span)
        l, r = self.ex(e.l), self.ex(e.r)
        return self.binop(e.op, e.opty, l, r, e.op_span)

    def incdec_value(self, t, x, op, span):
        """C expression for x+1 / x-1 (checked for integers)."""
        if t.is_float:
            return f"({x} {op[0]} {self.float_lit(1.0, t)})"
        name = "add" if op == "++" else "sub"
        return f"cb_{name}_{t.name}({x}, {self.int_lit(1, t)}, {self.loc(span)})"

    def g_IncDec(self, e):
        t, op = e.ty, e.op
        o = self.tmp("cb_o")
        if self.is_simple(e.expr):
            x = self.ex(e.expr)
            new = self.incdec_value(t, x, op, e.span)
            if e.prefix:
                return f"({{ {x} = {new}; {x}; }})"
            return f"({{ {self.ctype(t)} {o} = {x}; {x} = {new}; {o}; }})"
        p = self.tmp("cb_p")
        ct = self.ctype(t)
        new = self.incdec_value(t, f"{o}", op, e.span)
        res = f"*{p}" if e.prefix else o
        return f"({{ {ct} *{p} = &{self.ex(e.expr)}; {ct} {o} = *{p}; *{p} = {new}; {res}; }})"

    def g_Cast(self, e):
        src, dst = e.expr.ty, e.ty
        x = self.ex(e.expr)
        if src == dst:
            return x
        if dst.is_int and src.is_float:
            return f"cb_f2i_{dst.name}((double){x})"
        if src.kind == "ptr" and dst.is_int:
            return f"(({self.ctype(dst)})(uintptr_t)({x}))"
        if src.is_int and dst.kind == "ptr":
            return f"(({self.ctype(dst)})(uintptr_t)({x}))"
        return f"(({self.ctype(dst)}){x})"

    def idx(self, x):
        return f"(int64_t)({self.ex(x)})"

    def g_Index(self, e):
        ot = e.obj.ty
        loc = self.loc(e.span)
        i = self.idx(e.idx)
        if ot.kind == "arr":
            return f"({self.ex(e.obj)}).v[cb_idx({i}, {ot.n}, {loc})]"
        if self.is_simple(e.obj):
            o = self.ex(e.obj)
            return f"({o}).ptr[cb_idx({i}, ({o}).len, {loc})]"
        t = self.tmp()
        return f"(*({{ {self.ctype(ot)} {t} = {self.ex(e.obj)}; &{t}.ptr[cb_idx({i}, {t}.len, {loc})]; }}))"

    def g_SliceExpr(self, e):
        ot = e.obj.ty
        loc = self.loc(e.span)
        t, lo, hi = self.tmp(), self.tmp("cb_lo"), self.tmp("cb_hi")
        lo_x = self.idx(e.lo) if e.lo is not None else "0"
        if ot.kind == "arr":
            hi_x = self.idx(e.hi) if e.hi is not None else str(ot.n)
            return (f"({{ {self.ctype(ot)} *{t} = &({self.ex(e.obj)}); int64_t {lo} = {lo_x}; int64_t {hi} = {hi_x}; "
                    f"cb_chk_rng({lo}, {hi}, {ot.n}, {loc}); "
                    f"({self.ctype(e.ty)}){{ {t}->v + {lo}, {hi} - {lo} }}; }})")
        hi_x = self.idx(e.hi) if e.hi is not None else f"{t}.len"
        return (f"({{ {self.ctype(ot)} {t} = {self.ex(e.obj)}; int64_t {lo} = {lo_x}; int64_t {hi} = {hi_x}; "
                f"cb_chk_rng({lo}, {hi}, {t}.len, {loc}); "
                f"({self.ctype(e.ty)}){{ {t}.ptr + {lo}, {hi} - {lo} }}; }})")

    def g_StructLit(self, e):
        inits = ", ".join(f".{self.fld(n)} = {self.ex(v)}" for n, _, v in e.fields)
        return f"(({e.sym.cname}){{{inits}}})"

    def g_ArrayLit(self, e):
        return f"(({self.ctype(e.ty)}){{{{{', '.join(self.ex(x) for x in e.elems)}}}}})"

    def g_ArrayRepeat(self, e):
        ct = self.ctype(e.ty)
        el = e.elem
        zero = (el.kind in ("Int", "Float") and el.value == 0) or (el.kind == "Bool" and not el.value)
        if zero:
            return f"(({ct}){{{{0}}}})"
        t, i = self.tmp(), self.tmp("cb_i")
        return (f"({{ {self.ctype(e.ty.inner)} {t}e = {self.ex(el)}; {ct} {t}; "
                f"for (int64_t {i} = 0; {i} < {e.n}; {i}++) {t}.v[{i}] = {t}e; {t}; }})")

    # ----------------------------------------------------------------- calls
    def g_Call(self, e):
        b = getattr(e, "builtin", None)
        if b:
            return getattr(self, "b_" + b)(e)
        om = getattr(e, "opt_method", None)
        if om:
            obj = e.callee.obj
            x = self.ex(obj)
            if om == "is_some":
                return f"(({x}).has)"
            if om == "is_none":
                return f"(!({x}).has)"
            t = self.tmp()
            if om == "unwrap":
                return (f'({{ {self.ctype(obj.ty)} {t} = {x}; '
                        f'if (CB_UNLIKELY(!{t}.has)) cb_panic({self.loc(e.span)}, "`unwrap` on an empty optional"); {t}.v; }})')
            return f"({{ {self.ctype(obj.ty)} {t} = {x}; {t}.has ? {t}.v : ({self.ex(e.args[0])}); }})"
        args = ", ".join(self.ex(a) for a in e.args)
        if getattr(e, "indirect", False):
            return f"{self.nn(self.ex(e.callee), e.callee.ty, e.span)}({args})"
        return f"{e.sym.cname}({args})"

    def print_one(self, a):
        t = a.ty
        x = self.ex(a)
        if t.is_int:
            return f"cb_print_i((long long)({x}))" if t.signed else f"cb_print_u((unsigned long long)({x}))"
        if t.is_float:
            return f"cb_print_{t.name}({x})"
        if t == BOOL:
            return f"cb_print_bool({x})"
        if t == STR:
            return f"cb_print_str({x})"
        if t == STRING:
            return f"cb_print_str(cb_string_view({x}))"
        if t == ERR:
            return f"cb_print_cstr(cb_err_names[{x}])"
        if t.kind == "enum":
            return f"cb_print_cstr({t.sym.cname}__names[{x}])"
        if t.kind in ("ptr", "fn"):
            return f"cb_print_ptr((const void *)({x}))"
        raise AssertionError(t)

    def b_print(self, e, nl=False):
        parts = [self.print_one(a) for a in e.args]
        if nl:
            parts.append("putchar('\\n')")
        parts.append("(void)0")
        return "(" + ", ".join(parts) + ")"

    def b_println(self, e):
        return self.b_print(e, nl=True)

    def b_to_str(self, e):
        a = e.args[0]
        t, x, loc = a.ty, self.ex(a), self.loc(e.span)
        if t.is_int:
            return f"cb_str_from_i((long long)({x}), {loc})" if t.signed else f"cb_str_from_u((unsigned long long)({x}), {loc})"
        if t.is_float:
            return f"cb_str_from_f((double)({x}), {1 if t.name == 'f32' else 0}, {loc})"
        if t == BOOL:
            return f'cb_str_view_c(({x}) ? "true" : "false")'
        if t == STR:
            return x
        if t == STRING:
            return f"cb_string_view({x})"
        if t == ERR:
            return f"cb_str_view_c(cb_err_names[{x}])"
        return f"cb_str_view_c({t.sym.cname}__names[{x}])"

    def b_panic(self, e):
        return f"cb_panic_str({self.loc(e.span)}, {self.ex(e.args[0])})"

    def b_assert(self, e):
        loc = self.loc(e.span)
        cond = self.ex(e.args[0])
        if len(e.args) == 2:
            fail = f"cb_panic_str({loc}, {self.ex(e.args[1])})"
        else:
            fail = f'cb_panic({loc}, "assertion failed")'
        return f"(CB_LIKELY({cond}) ? (void)0 : {fail})"

    def b_exit(self, e):
        return f"exit({self.ex(e.args[0])})"

    def b_alloc(self, e):
        n = self.tmp("cb_n")
        et = self.ctype(e.elem)
        atomic = 1 if self.pointer_free(e.elem) else 0
        return (f"({{ int64_t {n} = (int64_t)({self.ex(e.args[0])}); "
                f"({self.ctype(e.ty)}){{ ({et} *)cb_alloc({n}, (int64_t)sizeof({et}), {atomic}, {self.loc(e.span)}), {n} }}; }})")

    def pointer_free(self, t):
        """True if values of type `t` can never contain a pointer (so the collector need not scan them)."""
        if t.kind == "prim":
            return t.is_numeric or t == BOOL
        if t.kind in ("enum", "err"):
            return True
        if t.kind == "arr":
            return self.pointer_free(t.inner)
        if t.kind == "struct":
            return all(self.pointer_free(ft) for _, ft, _ in t.sym.fields) if not t.sym.opaque else False
        return False

    def b_alloc_in(self, e):
        n = self.tmp("cb_n")
        et = self.ctype(e.elem)
        return (f"({{ int64_t {n} = (int64_t)({self.ex(e.args[0])}); "
                f"({self.ctype(e.ty)}){{ ({et} *)cb_region_alloc({e.region_local.cname}, {n}, (int64_t)sizeof({et}), {self.loc(e.span)}), {n} }}; }})")

    def b_gc_collections(self, e):
        return "cb_gc_collections()"

    def b_free(self, e):
        if e.args[0].ty == STRING:
            return f"cb_string_free({self.ex(e.args[0])})"
        return f"cb_mem_free(({self.ex(e.args[0])}).ptr)"

    def b_cstr(self, e):
        return f"cb_cstr({self.sv(e.args[0])}, {self.loc(e.span)})"

    def b_from_cstr(self, e):
        return f"cb_from_cstr((const char *)({self.ex(e.args[0])}))"

    def b_slice_of(self, e):
        return (f"(({self.ctype(e.ty)}){{ {self.ex(e.args[0])}, "
                f"cb_slice_len((int64_t)({self.ex(e.args[1])}), {self.loc(e.span)}) }})")

    def b_size_of(self, e):
        return f"((int64_t)sizeof({self.ctype(e.elem)}))"

    def b_drop(self, e):
        a = e.args[0]
        if self.ck.has_glue(a.ty):
            t = self.tmp()
            return f"({{ {self.ctype(a.ty)} {t} = {self.ex(a)}; {self.glue_fn(a.ty)}(&{t}); (void)0; }})"
        if a.ty == STRING:
            return f"cb_string_free({self.ex(a)})"
        return f"((void)({self.ex(a)}))"

    def b_drop_at(self, e):
        a = e.args[0]
        inner = a.ty.inner
        p = self.nn(self.ex(a), a.ty, e.span)
        if self.ck.has_glue(inner):
            return f"{self.glue_fn(inner)}({p})"
        if inner == STRING:
            q = self.tmp("cb_q")
            return f"({{ cb_String *{q} = {p}; cb_string_free(*{q}); *{q} = (cb_String){{0, 0, 0}}; (void)0; }})"
        return f"((void)({p}))"

    def b_write_at(self, e):
        a, v = e.args
        if self.pointer_free(v.ty):
            return f"((void)(*{self.nn(self.ex(a), a.ty, e.span)} = {self.ex(v)}))"
        p = self.tmp("cb_p")
        return (f"({{ {self.ctype(a.ty)} {p} = {self.nn(self.ex(a), a.ty, e.span)}; "
                f"*{p} = {self.ex(v)}; cb_wb_range({p}, sizeof *{p}); (void)0; }})")

    def b_read_at(self, e):
        a = e.args[0]
        return f"(*{self.nn(self.ex(a), a.ty, e.span)})"

    def b_gc_collect(self, e):
        return "cb_gc_collect()"

    def b_gc_bytes(self, e):
        return "cb_gc_bytes()"

    def b_gc_collect_gen(self, e):
        return f"cb_gc_collect_gen((int64_t)({self.ex(e.args[0])}), {self.loc(e.span)})"

    def b_gc_gen_bytes(self, e):
        return f"cb_gc_gen_bytes((int64_t)({self.ex(e.args[0])}), {self.loc(e.span)})"

    def b_gc_gen_collections(self, e):
        return f"cb_gc_gen_collections((int64_t)({self.ex(e.args[0])}), {self.loc(e.span)})"

    def b_string_new(self, e):
        return "((cb_String){0, 0, 0})"

    def b_string_from(self, e):
        return f"cb_string_from({self.ex(e.args[0])}, {self.loc(e.span)})"

    def b_format(self, e):
        return f"cb_string_from({self.ex(e.args[0])}, {self.loc(e.span)})"

    def b_string_clear(self, e):
        return f"((void)({self.nn(self.ex(e.args[0]), e.args[0].ty, e.span)}->len = 0))"

    # ---------------------------------------------------------------- errors
    def g_Try(self, e):
        return self.try_wrap(self.ex(e.expr), e.expr.ty)

    def fail_jump(self, err):
        """C statements that leave the current function (or the innermost `try { }`) with error code `err`."""
        if self.try_stack:
            var, label, base = self.try_stack[-1]
            return f"{var} = {err}; {' '.join(self.drops_text(base))} goto {label};"
        R = self.ctype(self.cur_ret)
        return f"{' '.join(self.drops_text(0))} return ({R}){{.err = {err}}};"

    def try_wrap(self, call, eut):
        t = self.tmp()
        val = "(void)0" if eut.inner == VOID else f"{t}.val"
        return (f"({{ {self.ctype(eut)} {t} = {call}; "
                f"if (CB_UNLIKELY({t}.err)) {{ {self.fail_jump(t + '.err')} }} {val}; }})")

    def s_TryBlock(self, s):
        n = self.tmp("cb_try")
        var, catch_l, end_l = f"{n}_err", f"{n}_catch", f"{n}_end"
        self.emit("{")
        self.ind += 1
        self.emit(f"int32_t {var} = 0;")
        self.try_stack.append((var, catch_l, len(self.dscopes)))
        self.emit("{")
        self.block(s.body)
        self.emit("}")
        self.try_stack.pop()
        self.emit(f"goto {end_l};")
        self.emit(f"{catch_l}: (void){var};")
        self.emit("{")
        self.ind += 1
        if s.binder_local is not None:
            self.emit(f"int32_t {s.binder_local.cname} = {var};")
        self.block_body(s.handler)
        self.ind -= 1
        self.emit("}")
        self.emit(f"{end_l}: ;")
        self.ind -= 1
        self.emit("}")

    def g_Ternary(self, e):
        return f"(({self.ex(e.cond)}) ? ({self.ex(e.a)}) : ({self.ex(e.b)}))"

    def g_OptPayload(self, e):
        raise AssertionError("payload placeholder")

    def g_Coalesce_(self, e):
        ot = e.l.ty
        t = self.tmp()
        if e.cmode == "ptr":
            return f"({{ {self.ctype(ot)} {t} = {self.ex(e.l)}; {t} ? {t} : ({self.ex(e.r)}); }})"
        if e.chain:
            return f"({{ {self.ctype(ot)} {t} = {self.ex(e.l)}; {t}.has ? {t} : ({self.ex(e.r)}); }})"
        return f"({{ {self.ctype(ot)} {t} = {self.ex(e.l)}; {t}.has ? {t}.v : ({self.ex(e.r)}); }})"

    def g_OptChain(self, e):
        ot = e.obj.ty
        t, r = self.tmp(), self.tmp()
        loc = e.payload_local
        rt = self.ctype(e.ty)
        if e.cmode == "opt":
            cond, bind = f"{t}.has", f"{self.ctype(ot.inner)} {loc.cname} = {t}.v;"
        else:
            cond, bind = f"{t} != NULL", f"{self.ctype(ot)} {loc.cname} = {t};"
        live = f"bool {loc.cname}__live = 0; (void){loc.cname}__live;"
        inner = self.ex(e.inner)
        val = inner if e.flat else f"(({rt}){{1, {inner}}})"
        return (f"({{ {self.ctype(ot)} {t} = {self.ex(e.obj)}; {rt} {r} = ({rt}){{0}}; "
                f"if ({cond}) {{ {bind} {live} {r} = {val}; }} {r}; }})")

    def g_Catch(self, e):
        t = self.tmp()
        lt = e.expr.ty
        T = lt.inner
        call = self.ex(e.expr)
        h = e.handler
        if h.kind != "Block":
            if getattr(e, "binder_local", None) is not None:
                return (f"({{ {self.ctype(lt)} {t} = {call}; {t}.err ? ({{ int32_t {e.binder_local.cname} = {t}.err; {self.ex(h)}; }}) : {t}.val; }})")
            return f"({{ {self.ctype(lt)} {t} = {call}; {t}.err ? ({self.ex(h)}) : {t}.val; }})"
        saved_ind = self.ind
        body_lines = []

        def gen():
            if e.binder_local is not None:
                self.emit(f"int32_t {e.binder_local.cname} = {t}.err;")
            self.block_body(h)

        self.ind = 0
        body = self.capture(gen).replace("\n", " ")
        self.ind = saved_ind
        val = "(void)0" if T == VOID else f"{t}.val"
        return f"({{ {self.ctype(lt)} {t} = {call}; if (CB_UNLIKELY({t}.err)) {{ {body} }} {val}; }})"

    # ------------------------------------------------------------ statements
    def block_body(self, b):
        self.dscopes.append([])
        for st in b.stmts:
            self.stmt(st)
        sc = self.dscopes.pop()
        for loc in reversed(sc):
            self.emit(self.drop_stmt(loc))

    def drop_stmt(self, loc):
        if getattr(loc, "region", False):
            return f"if ({loc.cname}__live) {{ cb_region_free({loc.cname}); {loc.cname}__live = 0; }}"
        return f"if ({loc.cname}__live) {self.glue_fn(loc.ty)}(&{loc.cname});"

    def s_Region(self, s):
        name = s.local.cname
        self.emit("{")
        self.ind += 1
        self.emit(f"cb_Region *{name} = cb_region_new(); bool {name}__live = 1;")
        mark = RegionLocal(name)
        self.dscopes.append([mark])
        self.block_body(s.body)
        self.dscopes.pop()
        self.emit(self.drop_stmt(mark))
        self.ind -= 1
        self.emit("}")

    def block(self, b):
        self.ind += 1
        self.block_body(b)
        self.ind -= 1

    TEMP_STMTS = ("Let", "ExprStmt", "Assign", "Return", "Throw", "If", "For")

    def stmt(self, s):
        saved = self.stmt_temps
        if s.kind not in self.TEMP_STMTS:
            self.stmt_temps = None
            getattr(self, "s_" + s.kind)(s)
            self.stmt_temps = saved
            return
        self.stmt_temps = []
        saved_out, self.out = self.out, []
        getattr(self, "s_" + s.kind)(s)
        body, self.out = self.out, saved_out
        temps, self.stmt_temps = self.stmt_temps, saved
        for t in temps:
            self.emit(f"{self.ctype(t.ty)} {t.cname} = {{0}}; bool {t.cname}__live = 0;")
        self.out += body
        for t in temps:
            if self.dscopes and t in self.dscopes[-1]:
                self.dscopes[-1].remove(t)
            self.emit(f"if ({t.cname}__live) {self.glue_fn(t.ty)}(&{t.cname});")

    def s_Block(self, s):
        self.emit("{")
        self.block(s)
        self.emit("}")

    # ---- destructors
    def glue_fn(self, t):
        """Name of the C function that destroys a value of type `t` (user destructor, then fields)."""
        got = self.glue_done.get(t.key)
        if got:
            return got
        name = f"cb_drop_{len(self.glue_done) + 1}"
        self.glue_done[t.key] = name
        self.glue_todo.append((name, t))
        return name

    def gen_glue(self, name, t):
        ct = self.ctype(t)
        L = [f"static void {name}({ct} *p) {{"]
        if t == STRING:
            L.append("    cb_string_free(*p);")
            L.append("    p->ptr = 0; p->len = 0; p->cap = 0;")
        elif t.kind == "arr":
            L.append(f"    for (int64_t i = 0; i < {t.n}; i++) {self.glue_fn(t.inner)}(&p->v[i]);")
        elif t.kind == "opt":
            L.append(f"    if (p->has) {self.glue_fn(t.inner)}(&p->v);")
        else:
            d = self.ck.drop_fn_of(t.sym)
            if d is not None:
                L.append(f"    {d.cname}(p);")
            for fname, ft, _ in t.sym.fields:
                if self.ck.has_glue(ft):
                    L.append(f"    {self.glue_fn(ft)}(&p->{self.fld(fname)});")
        L.append("}")
        return "\n".join(L)

    def drops_text(self, base):
        """C statements destroying every live local in scopes `base..` (innermost first)."""
        out = []
        for sc in reversed(self.dscopes[base:]):
            for loc in reversed(sc):
                out.append(self.drop_stmt(loc))
        return out

    def drop_value(self, t, x):
        v = self.tmp()
        self.emit("{")
        self.emit(f"    {self.ctype(t)} {v} = {x};")
        self.emit(f"    {self.glue_fn(t)}(&{v});")
        self.emit("}")

    def s_Let(self, s):
        if s.local is None:
            if getattr(s, "drop_temp", False):
                self.drop_value(s.init.ty, self.ex(s.init))
            else:
                self.emit(f"(void)({self.ex(s.init)});")
            return
        self.emit(f"{self.ctype(s.local.ty)} {s.local.cname} = {self.ex(s.init)};")
        if s.local.glue:
            self.emit(f"bool {s.local.cname}__live = 1;")
            self.dscopes[-1].append(s.local)

    def s_ExprStmt(self, s):
        e = s.expr
        if getattr(s, "drop_temp", False):
            self.drop_value(e.ty, self.ex(e))
            return
        if e.kind == "IncDec" and self.is_simple(e.expr):      # plain `i++;` -> `i = i + 1;`
            x = self.ex(e.expr)
            self.emit(f"{x} = {self.incdec_value(e.ty, x, e.op, e.span)};")
            return
        self.emit(f"(void)({self.ex(e)});")

    def wb_target(self, t):
        """True if a store through the lvalue `t` can land in heap memory (so the write barrier must run)."""
        k = t.kind
        if k == "Ident":
            return False
        if k == "Field":
            return True if getattr(t, "mode", None) != "field" else self.wb_target(t.obj)
        if k == "Index":
            return True if t.obj.ty.kind != "arr" else self.wb_target(t.obj)
        return True

    def s_Assign(self, s):
        t = s.target
        rhs = self.ex(s.value)
        wb = self.wb_target(t) and not self.pointer_free(s.opty)       # the generational collector's write barrier
        if s.op == "=":
            if self.ck.has_glue(s.opty):        # destroy the old value, then store the new one
                ct, g, v = self.ctype(s.opty), self.glue_fn(s.opty), self.tmp()
                loc = getattr(t, "local", None) if t.kind == "Ident" else None
                self.emit("{")
                self.emit(f"    {ct} {v} = {rhs};")
                if loc is not None:
                    self.emit(f"    if ({loc.cname}__live) {g}(&{loc.cname});")
                    self.emit(f"    {loc.cname} = {v};")
                    self.emit(f"    {loc.cname}__live = 1;")
                else:
                    p = self.tmp("cb_p")
                    self.emit(f"    {ct} *{p} = &{self.ex(t)};")
                    self.emit(f"    {g}({p});")
                    self.emit(f"    *{p} = {v};")
                    if wb:
                        self.emit(f"    cb_wb_range({p}, sizeof *{p});")
                self.emit("}")
                return
            if wb and not self.is_simple(t):
                p = self.tmp("cb_p")
                self.emit("{")
                self.emit(f"    {self.ctype(s.opty)} *{p} = &{self.ex(t)};")
                self.emit(f"    *{p} = {rhs};")
                self.emit(f"    cb_wb_range({p}, sizeof *{p});")
                self.emit("}")
                return
            self.emit(f"{self.ex(t)} = {rhs};")
            if wb:
                self.emit(f"cb_wb_range(&{self.ex(t)}, sizeof {self.ex(t)});")
            return
        if s.opty in (STR, STRING):
            loc = self.loc(s.op_span)

            def combine(lhs):
                if s.opty == STRING:
                    if s.value.ty == STRING or s.value.ty == STR:
                        return f"cb_string_append({lhs}, {self.sv(s.value)}, {loc})"
                    return f"cb_string_push({lhs}, {rhs}, {loc})"
                return f"cb_str_catv((cb_str[]){{{lhs}, {self.sv(s.value)}}}, 2, {loc})"
        else:
            wb = False

            def combine(lhs):
                return self.binop(s.bop, s.opty, lhs, rhs, s.op_span)
        if self.is_simple(t):
            lhs = self.ex(t)
            self.emit(f"{lhs} = {combine(lhs)};")
            if wb:
                self.emit(f"cb_wb_range(&{lhs}, sizeof {lhs});")
            return
        p = self.tmp("cb_p")
        self.emit("{")
        self.ind += 1
        self.emit(f"{self.ctype(s.opty)} *{p} = &{self.ex(t)};")
        self.emit(f"*{p} = {combine('*' + p)};")
        if wb:
            self.emit(f"cb_wb_range({p}, sizeof *{p});")
        self.ind -= 1
        self.emit("}")

    def s_If(self, s, first=True):
        self.emit(f"if ({self.strip(self.ex(s.cond))}) {{")
        self.block(s.then)
        els = s.els
        while els is not None and els.kind == "If":
            self.emit(f"}} else if ({self.strip(self.ex(els.cond))}) {{")
            self.block(els.then)
            els = els.els
        if els is not None:
            self.emit("} else {")
            self.block(els)
        self.emit("}")

    def s_While(self, s):
        self.emit(f"while ({self.strip(self.ex(s.cond))}) {{")
        self.poll()
        self.loop_bases.append(len(self.dscopes))
        self.block(s.body)
        self.loop_bases.pop()
        self.emit("}")

    def s_Loop(self, s):
        self.emit("for (;;) {")
        self.poll()
        self.loop_bases.append(len(self.dscopes))
        self.block(s.body)
        self.loop_bases.pop()
        self.emit("}")

    def s_Break(self, s):
        for d in self.drops_text(self.loop_bases[-1] if self.loop_bases else 0):
            self.emit(d)
        self.emit("break;")

    def s_Continue(self, s):
        for d in self.drops_text(self.loop_bases[-1] if self.loop_bases else 0):
            self.emit(d)
        self.emit("continue;")

    def finish_return(self, value):
        """Emit `return value;`, destroying live locals first (after the value is computed)."""
        drops = self.drops_text(0)
        if not drops:
            self.emit(f"return {value};" if value else "return;")
            return
        self.emit("{")
        if value:
            v = self.tmp()
            self.emit(f"    {self.ctype(self.cur_ret)} {v} = {value};")
        for d in drops:
            self.emit("    " + d)
        self.emit(f"    return {v};" if value else "    return;")
        self.emit("}")

    def s_Return(self, s):
        ret = self.cur_ret
        if ret.kind == "eu":
            R = self.ctype(ret)
            if s.expr is None or ret.inner == VOID:
                self.finish_return(f"({R}){{0}}")
            else:
                self.finish_return(f"({R}){{.err = 0, .val = {self.ex(s.expr)}}}")
        elif s.expr is None:
            self.finish_return(None)
        else:
            self.finish_return(self.ex(s.expr))

    def s_Throw(self, s):
        if self.try_stack:
            self.emit("{ " + self.fail_jump(self.ex(s.expr)) + " }")
            return
        self.finish_return(f"({self.ctype(self.cur_ret)}){{.err = {self.ex(s.expr)}}}")

    def s_For(self, s):
        it = s.iter
        var = s.local.cname if s.local is not None else self.tmp("cb_v")
        self.emit("{")
        self.ind += 1
        if s.mode == "range":
            T = self.ctype(s.range_ty)
            lo, hi = self.tmp("cb_lo"), self.tmp("cb_hi")
            self.emit(f"{T} {lo} = {self.ex(it.lo)};")
            self.emit(f"{T} {hi} = {self.ex(it.hi)};")
            if not it.inclusive:
                self.emit(f"for ({T} {var} = {lo}; {var} < {hi}; {var}++) {{")
            else:
                go = self.tmp("cb_go")
                self.emit(f"bool {go} = {lo} <= {hi};")
                self.emit(f"for ({T} {var} = {lo}; {go}; {go} = ({var} != {hi}), {var} = ({T})({var} + {go})) {{")
        else:
            ot = it.ty
            p, n, i = self.tmp("cb_p"), self.tmp("cb_n"), self.tmp("cb_i")
            et = self.ctype(ot.inner) if ot.kind != "prim" else "uint8_t"
            if ot.kind == "arr":
                a = self.tmp("cb_a")
                if self.is_place(it):
                    self.emit(f"{self.ctype(ot)} *{a} = &{self.ex(it)};")
                    self.emit(f"{et} *{p} = {a}->v;")
                else:
                    self.emit(f"{self.ctype(ot)} {a} = {self.ex(it)};")
                    self.emit(f"{et} *{p} = {a}.v;")
                self.emit(f"int64_t {n} = {ot.n};")
            else:
                sv = self.tmp("cb_s")
                self.emit(f"{self.ctype(ot)} {sv} = {self.ex(it)};")
                self.emit(f"const {et} *{p} = {sv}.ptr;" if ot == STR else f"{et} *{p} = {sv}.ptr;")
                self.emit(f"int64_t {n} = {sv}.len;")
            self.emit(f"for (int64_t {i} = 0; {i} < {n}; {i}++) {{")
            if s.local is not None:
                self.emit(f"    {et} {var} = {p}[{i}];")
        self.poll()
        self.loop_bases.append(len(self.dscopes))
        self.block(s.body)
        self.loop_bases.pop()
        self.emit("}")
        self.ind -= 1
        self.emit("}")

    def s_Match(self, s):
        st = s.scrut_ty
        t = self.tmp("cb_m")
        self.emit("{")
        self.ind += 1
        self.emit(f"{self.ctype(st)} {t} = {self.ex(s.scrut)};")
        first = True
        for arm in s.arms:
            if arm.wild:
                cond = "1"
            else:
                cond = " || ".join(f"{t} == {self.ex(p)}" for p in arm.pats)
            kw = "if" if first else "} else if"
            self.emit(f"{kw} ({cond}) {{")
            self.block(arm.body)
            first = False
        if not first:
            self.emit("}")
        else:
            self.emit(f"(void){t};")
        self.ind -= 1
        self.emit("}")

    # ------------------------------------------------------------- top level
    def sig(self, sym):
        ret = self.ctype(sym.ret) if sym.ret != VOID else "void"
        params = ", ".join(f"{self.ctype(t)} {l.cname}" for t, l in zip(sym.params, getattr(sym, "param_locals", [])))
        return f"{'' if sym.export else 'static '}{ret} {sym.cname}({params or 'void'})"

    def gen_fn(self, sym):
        self.cur_ret = sym.ret
        self.ind = 1
        self.out = []
        self.dscopes = [[]]
        self.loop_bases = []
        self.try_stack = []
        self.poll()
        for loc in getattr(sym, "param_locals", []):
            if loc.glue:
                self.emit(f"bool {loc.cname}__live = 1;")
                self.dscopes[0].append(loc)
        self.block_body(sym.node.body)
        for loc in reversed(self.dscopes[0]):
            self.emit(self.drop_stmt(loc))
        if sym.ret.kind == "eu":
            self.emit(f"return ({self.ctype(sym.ret)}){{0}};" if sym.ret.inner == VOID else "__builtin_unreachable();")
        elif sym.ret != VOID:
            self.emit("__builtin_unreachable();")
        return f"{self.sig(sym)} {{\n" + "\n".join(self.out) + "\n}\n"

    def const_def(self, sym):
        v = sym.value
        t = sym.ty
        ct = self.ctype(t)
        if t.is_int:
            lit = self.int_lit(v, t)
        elif t.is_float:
            lit = self.float_lit(v, t)
        elif t == BOOL:
            lit = "true" if v else "false"
        else:
            lit = f"{{(const uint8_t *){c_string(v)}, {len(v)}}}"
        return f"CB_UNUSED static const {ct} {sym.cname} = {lit};"

    def prune(self, fns, bodies, protos, glue):
        """Drop standard-library functions nothing refers to (the prelude pulls in all of std.str / std.list).
        Reachability is computed on the generated text: any identifier that names a function counts as a use."""
        import re
        ident = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
        chunks = {}                                  # cname -> (kind, index)
        for i, s in enumerate(fns):
            chunks[s.cname] = ("fn", i)
        for i, g in enumerate(glue):
            m = re.search(r"\b(cb_drop_[A-Za-z0-9_]+)\s*\(", g)
            if m:
                chunks[m.group(1)] = ("glue", i)
        keep_fn, keep_glue = set(), set()
        work = []

        def mark(name):
            ent = chunks.get(name)
            if ent is None:
                return
            kind, i = ent
            seen = keep_fn if kind == "fn" else keep_glue
            if i not in seen:
                seen.add(i)
                work.append((kind, i))
        ck = self.ck
        for i, s in enumerate(fns):
            if not s.mod.is_std or s.export:
                mark(s.cname)
        if ck.main_sym is not None:
            mark(ck.main_sym.cname)
        # glue and constants referenced from user code or from emitted structs
        extra = "\n".join(self.const_def(c) for c in ck.consts if not c.extern)
        for tok in set(ident.findall(extra)):
            mark(tok)
        while work:
            kind, i = work.pop()
            text = bodies[i] if kind == "fn" else glue[i]
            for tok in set(ident.findall(text)):
                mark(tok)
        # anything the entry-less library mode exposes is already kept (non-std and exported)
        keep = sorted(keep_fn)
        return ([fns[i] for i in keep], [bodies[i] for i in keep], [protos[i] for i in keep],
                [g for i, g in enumerate(glue) if i in keep_glue])

    def generate(self, src_name):
        with TIMINGS.phase("codegen"):
            out = self._generate(src_name)
        TIMINGS.add("c_lines", out.count("\n"))
        TIMINGS.add("c_bytes", len(out))
        return out

    def _generate(self, src_name):
        ck = self.ck
        fns = [s for s in ck.fns if not s.extern]
        bodies = [self.gen_fn(s) for s in fns]
        protos = [self.sig(s) + ";" for s in fns]
        glue = []                # destructor glue (generating one may require more)
        while self.glue_todo:
            name, t = self.glue_todo.pop(0)
            glue.append(self.gen_glue(name, t))
        dbg = []
        while self.dbg_todo:      # generating one may need more (fields), and the glue/debug passes feed each other
            name, t = self.dbg_todo.pop(0)
            dbg.append(self.gen_dbg(name, t))
        fns, bodies, protos, glue = self.prune(fns, bodies, protos, glue)
        for s in ck.fns:     # private prototypes for user-declared C functions, bound to the real symbol
            if s.extern:
                ps = [self.ctype(t) for t in s.params] + (["..."] if s.variadic else [])
                ret = self.ctype(s.ret) if s.ret != VOID else "void"
                protos.append(f"extern {ret} {s.cname}({', '.join(ps) or 'void'}) CB_ASM({c_string(s.csym.encode())});")
        consts = [self.const_def(s) for s in ck.consts if not s.extern]
        # make sure every struct field type is registered
        for s in ck.structs:
            for _, t, _ in s.fields:
                self.ctype(t)

        defines, includes = [], []
        for m in ck.mods:
            for kind, val, _ in m.directives:
                if kind == "define":
                    line = "#define " + val.replace("=", " ", 1)
                    if line not in defines:
                        defines.append(line)
                elif kind == "include":
                    line = f'#include "{val}"'
                    if line not in includes:
                        includes.append(line)
        modedef = {"manual": ["#define CB_GC_MANUAL 1"],
                   "thread": ["#define CB_GC_THREAD 1", f"#define CB_GC_INTERVAL_MS {self.gc_interval}"]}.get(self.gc_mode, []) if self.gc else []
        gcdef = (["#define CB_GC 1"] + modedef if self.gc else []) + (["#define CB_NOSTD 1"] if self.nostd else []) \
            + (["#define CB_OVERFLOW_PANIC 1"] if self.overflow_panic else [])
        L = [f"/* Generated by Crumbs from {src_name}. Do not edit. */"] + gcdef + defines + [RUNTIME] + includes

        # forward declarations
        comps = {}
        for s in ck.structs:
            comps[("s", s.full)] = s
        for name, t, _ in self.arrs:
            comps[("a", t.key)] = (name, t)
        for name, t in self.opts:
            comps[("o", t.key)] = (name, t)
        fwd = [f"typedef struct {s.cname} {s.cname};" for s in ck.structs]
        fwd += [f"typedef struct {name} {name};" for name, _, _ in self.arrs]
        fwd += [f"typedef struct {name} {name};" for name, _, _ in self.slices]
        fwd += [f"typedef struct {name} {name};" for name, _, _ in self.eus]
        fwd += [f"typedef struct {name} {name};" for name, _ in self.opts]
        L.append("/* forward declarations */")
        L += fwd
        L.append("")

        # enums
        if ck.enums:
            L.append("/* enums */")
        for s in ck.enums:
            L.append(f"typedef int32_t {s.cname};")
            L.append("enum { " + ", ".join(f"{s.cname}__{v} = {i}" for i, v in enumerate(s.variants)) + " };")
            L.append(f"CB_UNUSED static const char *const {s.cname}__names[] = {{"
                     + ", ".join(c_string(v.encode()) for v in s.variants) + "};")
        L.append("")

        # error codes
        L.append("/* errors */")
        if ck.errors:
            L.append("enum { " + ", ".join(f"{s.cname} = {s.code}" for s in ck.errors) + " };")
        names = ['"<none>"'] + [c_string(f"{s.mod.name}.{s.name}".encode()) for s in ck.errors]
        L.append("CB_UNUSED static const char *const cb_err_names[] = {" + ", ".join(names) + "};")
        L.append("")

        # function pointer types, then slices (which only need pointers to element types)
        L.append("/* function pointer types */")
        L += self.fnts
        L.append("/* slices */")
        for name, t, inner in self.slices:
            L.append(f"struct {name} {{ {inner} *ptr; int64_t len; }};")
        L.append("")

        # composite value types in dependency order
        L.append("/* structs and arrays */")
        done = set()

        def deps_of(t):
            if t.kind == "struct":
                yield ("s", t.sym.full)
            elif t.kind == "arr":
                yield ("a", t.key)
            elif t.kind == "opt":
                yield ("o", t.key)

        def emit_comp(key):
            if key in done:
                return
            done.add(key)
            node = comps.get(key)
            if node is None:
                return
            if key[0] == "s":
                if node.opaque:
                    return
                for _, ft, _ in node.fields:
                    for d in deps_of(ft):
                        emit_comp(d)
                body = " ".join(f"{self.ctype(ft)} {self.fld(fn)};" for fn, ft, _ in node.fields)
                L.append(f"struct {node.cname} {{ {body} }};")
            elif key[0] == "o":
                name, t = node
                for d in deps_of(t.inner):
                    emit_comp(d)
                L.append(f"struct {name} {{ bool has; {self.ctype(t.inner)} v; }};")
            else:
                name, t = node
                for d in deps_of(t.inner):
                    emit_comp(d)
                L.append(f"struct {name} {{ {self.ctype(t.inner)} v[{t.n}]; }};")

        for key in list(comps):
            emit_comp(key)
        L.append("")

        # error unions
        L.append("/* error unions */")
        for name, t, inner in self.eus:
            if inner is None:
                L.append(f"struct {name} {{ int32_t err; }};")
            else:
                L.append(f"struct {name} {{ int32_t err; {inner} val; }};")
        L.append("")

        L.append("/* constants */")
        L += consts
        L.append("")
        L.append("/* prototypes */")
        L += protos
        L.append("")
        if glue:
            L.append("/* destructors */")
            L += [g.split("\n", 1)[0].rstrip(" {") + ";" for g in glue]     # glue functions call each other in any order
            L += glue
            L.append("")
        if dbg:
            L.append("/* debug representations */")
            L += [g.split("\n", 1)[0].rstrip(" {") + ";" for g in dbg]
            L += dbg
            L.append("")
        L.append("/* functions */")
        L += bodies

        # entry point
        m = ck.main_sym
        if m is not None:
            r = m.ret
            L.append("int main(void) {" if self.nostd else "int main(int argc, char **argv) {")
            if not self.nostd:
                L.append("    cb_argc_v = argc; cb_argv_v = argv;")
                L.append("    CB_GC_INIT();")
            if r.kind == "eu":
                L.append(f"    {self.ctype(r)} r = {m.cname}();")
                L.append("    if (r.err) return 1;" if self.nostd else "    if (r.err) { fflush(stdout); fprintf(stderr, \"error: %s\\n\", cb_err_names[r.err]); return 1; }")
                L.append("    return " + ("0;" if r.inner == VOID else "(int)r.val;"))
            elif r == VOID:
                L.append(f"    {m.cname}();")
                L.append("    return 0;")
            else:
                L.append(f"    return (int){m.cname}();")
            L.append("}")
        return "\n".join(L) + "\n"
