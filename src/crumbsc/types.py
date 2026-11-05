"""Type representation for Crumbs."""

INTS = {
    "i8": (8, True), "i16": (16, True), "i32": (32, True), "i64": (64, True),
    "u8": (8, False), "u16": (16, False), "u32": (32, False), "u64": (64, False),
}
FLOATS = {"f32", "f64"}


class Ty:
    __slots__ = ("kind", "name", "inner", "n", "sym", "key", "params", "ro")

    def __init__(self, kind, name=None, inner=None, n=None, sym=None, params=None):
        self.kind = kind
        self.name = name
        self.inner = inner      # pointee / element / return type
        self.n = n
        self.sym = sym
        self.params = params    # parameter types of a `fn` type
        self.ro = False         # a `*const T` pointer: same type as `*T` for identity, but nothing may be written through it
        if kind == "prim":
            self.key = name
        elif kind == "ptr":
            self.key = "*" + inner.key
        elif kind == "arr":
            self.key = f"[{n}]" + inner.key
        elif kind == "slice":
            self.key = "[]" + inner.key
        elif kind == "eu":
            self.key = inner.key + "!"
        elif kind == "opt":
            self.key = inner.key + "?"
        elif kind == "fn":
            self.key = "fn(" + ",".join(p.key for p in params) + ")->" + inner.key
        elif kind == "clo":
            self.key = "clo(" + ",".join(p.key for p in params) + ")->" + inner.key
        elif kind in ("struct", "enum"):
            self.key = f"{kind}:{sym.full}"
        else:
            self.key = kind

    def __eq__(self, o):
        return isinstance(o, Ty) and self.key == o.key

    def __hash__(self):
        return hash(self.key)

    def __str__(self):
        k = self.kind
        if k == "prim":
            return self.name
        if k == "ptr":
            return ("*const " if self.ro else "*") + str(self.inner)
        if k == "arr":
            return f"[{self.n}]{self.inner}"
        if k == "slice":
            return f"[]{self.inner}"
        if k == "eu":
            return f"{self.inner}!"
        if k == "opt":
            return f"{self.inner}?"
        if k == "fn":
            ret = "" if self.inner.kind == "prim" and self.inner.name == "void" else f" -> {self.inner}"
            return f"fn({', '.join(str(p) for p in self.params)}){ret}"
        if k == "clo":
            ret = "" if self.inner.kind == "prim" and self.inner.name == "void" else f" -> {self.inner}"
            return f"|{', '.join(str(p) for p in self.params)}|{ret}"
        if k in ("struct", "enum"):
            return self.sym.name
        return self.kind

    __repr__ = __str__

    # --- predicates
    @property
    def is_int(self):
        return self.kind == "prim" and self.name in INTS

    @property
    def is_float(self):
        return self.kind == "prim" and self.name in FLOATS

    @property
    def is_numeric(self):
        return self.is_int or self.is_float

    @property
    def signed(self):
        return INTS[self.name][1]

    @property
    def bits(self):
        return INTS[self.name][0]

    def int_range(self):
        b, s = INTS[self.name]
        return (-(1 << (b - 1)), (1 << (b - 1)) - 1) if s else (0, (1 << b) - 1)

    @property
    def poison(self):
        return self.kind == "poison"


PRIMS = {}
for _n in list(INTS) + ["f32", "f64", "bool", "str", "String", "void"]:
    PRIMS[_n] = Ty("prim", name=_n)

I8, I16, I32, I64 = PRIMS["i8"], PRIMS["i16"], PRIMS["i32"], PRIMS["i64"]
U8, U16, U32, U64 = PRIMS["u8"], PRIMS["u16"], PRIMS["u32"], PRIMS["u64"]
F32, F64 = PRIMS["f32"], PRIMS["f64"]
BOOL, STR, VOID = PRIMS["bool"], PRIMS["str"], PRIMS["void"]
STRING = PRIMS["String"]
REGION = Ty("prim", name="Region")   # the handle introduced by `region r { ... }`; not nameable in source
ERR = Ty("err")          # the `error` type: an error code
POISON = Ty("poison")    # result of an earlier error; compatible with everything
NEVER = Ty("never")


def ptr(t, ro=False):
    p = Ty("ptr", inner=t)
    p.ro = ro
    return p


def arr(t, n):
    return Ty("arr", inner=t, n=n)


def slc(t):
    return Ty("slice", inner=t)


def fnty(params, ret):
    return Ty("fn", inner=ret, params=tuple(params))


def clo(params, ret):
    return Ty("clo", inner=ret, params=tuple(params))


def eu(t):
    return Ty("eu", inner=t)


def opt(t):
    return Ty("opt", inner=t)


def same(a, b):
    return a.kind == "poison" or b.kind == "poison" or a == b
