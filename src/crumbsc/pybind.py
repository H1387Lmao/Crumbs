"""Python extension target: wrappers that expose a Crumbs module's `pub fn`s through the CPython C API.

The wrapper code is appended to the generated C (by CodeGen) so it can call the compiled functions directly and use the
C layout of every type. Conversions are generated per type:

    int types / bool / f32 / f64      int / bool / float
    str, String                       str (UTF-8)
    T?                                None or T
    List<T>, []T (results), [N]T      list
    struct (all fields convertible)   dict (arguments also accept any object with the attributes)
    enum                              int (the variant's number)
    T!                                T, or raises `<module>.CrumbsError` carrying the error's name
    void                              None

A runtime panic raises `<module>.Panic` instead of ending the process. Anything else (pointers, closures, function
pointers, generics, methods) is not exposed; the build says which function was skipped and why.
"""
from .types import VOID, STRING, BOOL


def c_str(s):
    out = []
    for b in s.encode("utf-8"):
        out.append(chr(b) if 32 <= b < 127 and chr(b) not in '"\\?' else "\\%03o" % b)
    return '"' + "".join(out) + '"'


class Unsupported(Exception):
    pass


class PyBind:
    def __init__(self, cg, modname, entry_mod):
        self.cg, self.ck, self.modname, self.mod = cg, cg.ck, modname, entry_mod
        self.to_n, self.from_n = {}, {}
        self.protos, self.bodies = [], []
        self.notes = []                  # (function name, why it is not exposed)
        self.exposed = []                # (Python name, sym) for the stub file
        self.stub_types = {}             # struct sym full name -> sym (TypedDicts for the .pyi)
        self.busy = set()

    # ---------------------------------------------------------------- type support
    def is_list(self, t):
        return t.kind == "struct" and t.sym.base is not None and t.sym.base.name == "List" and t.sym.base.mod.name == "std.list"

    def check(self, t, top_ret=False):
        """Raises Unsupported(why) unless values of type `t` can cross the boundary."""
        k = t.kind
        if k == "prim":
            if t == VOID and not top_ret:
                raise Unsupported("`void` is only valid as a result")
            return
        if k == "enum":
            return
        if k == "opt":
            return self.check(t.inner)
        if k == "arr":
            return self.check(t.inner)
        if k == "slice":
            return self.check(t.inner)
        if k == "eu":
            if not top_ret:
                raise Unsupported("error unions (`T!`) are only supported as a function's result")
            if t.inner != VOID:
                self.check(t.inner)
            return
        if k == "struct":
            if self.is_list(t):
                return self.check(t.sym.targs[0])
            if t.sym.extern_struct or t.sym.opaque:
                raise Unsupported(f"the C struct `{t.sym.name}` has no Python form")
            if t.key in self.busy:
                raise Unsupported(f"the recursive struct `{t.sym.name}`")
            self.busy.add(t.key)
            try:
                for fname, ft, _ in t.sym.fields:
                    try:
                        self.check(ft)
                    except Unsupported as e:
                        raise Unsupported(f"field `{fname}` of `{t.sym.name}`: {e}")
            finally:
                self.busy.discard(t.key)
            return
        raise Unsupported(f"the type `{t}` has no Python form")

    # ---------------------------------------------------------------- converters
    def conv_from(self, t):
        """Name of `PyObject *f(T v)` building a Python value from a C value (the C value stays owned by the caller)."""
        if t.key in self.from_n:
            return self.from_n[t.key]
        name = f"pyf_{len(self.from_n) + len(self.to_n) + 1}"
        self.from_n[t.key] = name
        ct = self.cg.ctype(t)
        self.protos.append(f"static PyObject *{name}({ct} v);")
        L = [f"static PyObject *{name}({ct} v) {{"]
        k = t.kind
        if k == "prim":
            if t == BOOL:
                L.append("    return PyBool_FromLong(v);")
            elif t.is_float:
                L.append("    return PyFloat_FromDouble((double)v);")
            elif t == STRING:
                L.append("    return PyUnicode_DecodeUTF8((const char *)v.ptr, (Py_ssize_t)v.len, \"replace\");")
            elif t.name == "str":
                L.append("    return PyUnicode_DecodeUTF8((const char *)v.ptr, (Py_ssize_t)v.len, \"replace\");")
            elif t.signed:
                L.append("    return PyLong_FromLongLong((long long)v);")
            else:
                L.append("    return PyLong_FromUnsignedLongLong((unsigned long long)v);")
        elif k == "enum":
            L.append("    return PyLong_FromLong((long)v);")
        elif k == "opt":
            f = self.conv_from(t.inner)
            L.append(f"    if (!v.has) Py_RETURN_NONE;")
            L.append(f"    return {f}(v.v);")
        elif k in ("arr", "slice", "struct") and (k != "struct" or self.is_list(t)):
            if k == "arr":
                n, ptr = str(t.n), "v.v"
            elif k == "slice":
                n, ptr = "v.len", "v.ptr"
            else:
                n, ptr = "v.len", "v.items.ptr"
            f = self.conv_from(t.inner if k != "struct" else t.sym.targs[0])
            L.append(f"    PyObject *l = PyList_New((Py_ssize_t)({n}));")
            L.append("    if (!l) return NULL;")
            L.append(f"    for (int64_t i = 0; i < (int64_t)({n}); i++) {{")
            L.append(f"        PyObject *x = {f}({ptr}[i]);")
            L.append("        if (!x) { Py_DECREF(l); return NULL; }")
            L.append("        PyList_SET_ITEM(l, (Py_ssize_t)i, x);")
            L.append("    }")
            L.append("    return l;")
        elif k == "struct":
            self.stub_types[t.sym.full] = t.sym
            L.append("    PyObject *d = PyDict_New();")
            L.append("    if (!d) return NULL;")
            for fname, ft, _ in t.sym.fields:
                f = self.conv_from(ft)
                L.append(f"    {{ PyObject *x = {f}(v.{self.cg.fld(fname)});")
                L.append(f"      if (!x || PyDict_SetItemString(d, {c_str(fname)}, x) < 0) {{ Py_XDECREF(x); Py_DECREF(d); return NULL; }}")
                L.append("      Py_DECREF(x); }")
            L.append("    return d;")
        else:
            raise Unsupported(f"no Python form for {t}")
        L.append("}")
        self.bodies.append("\n".join(L))
        return name

    def drop_stmt(self, t, expr):
        return f"{self.cg.glue_fn(t)}(&{expr});" if self.ck.has_glue(t) else ""

    def conv_to(self, t):
        """Name of `int f(PyObject *o, T *out)` (0 on success, -1 with a Python error set). Owned results (String, List,
        structs holding them) belong to the caller afterwards."""
        if t.key in self.to_n:
            return self.to_n[t.key]
        name = f"pyt_{len(self.from_n) + len(self.to_n) + 1}"
        self.to_n[t.key] = name
        ct = self.cg.ctype(t)
        self.protos.append(f"static int {name}(PyObject *o, {ct} *out);")
        L = [f"static int {name}(PyObject *o, {ct} *out) {{"]
        k = t.kind
        if k == "prim":
            if t == BOOL:
                L += ["    int b = PyObject_IsTrue(o);", "    if (b < 0) return -1;", "    *out = b != 0;"]
            elif t.is_float:
                L += ["    double x = PyFloat_AsDouble(o);", "    if (x == -1.0 && PyErr_Occurred()) return -1;", f"    *out = ({ct})x;"]
            elif t == STRING:
                L += ["    Py_ssize_t n; const char *p = PyUnicode_AsUTF8AndSize(o, &n);", "    if (!p) return -1;",
                      "    *out = cb_string_from((cb_str){ (const uint8_t *)p, (int64_t)n }, \"python\");"]
            elif t.name == "str":
                L += ["    Py_ssize_t n; const char *p = PyUnicode_AsUTF8AndSize(o, &n);", "    if (!p) return -1;",
                      "    *out = (cb_str){ (const uint8_t *)p, (int64_t)n };"]
            else:
                lo, hi = t.int_range()
                if t.signed:
                    L += ["    long long x = PyLong_AsLongLong(o);", "    if (x == -1 && PyErr_Occurred()) return -1;"]
                    if t.name != "i64":
                        L += [f"    if (x < {lo}LL || x > {hi}LL) {{ PyErr_SetString(PyExc_OverflowError, \"value out of range for {t.name}\"); return -1; }}"]
                else:
                    L += ["    unsigned long long x = PyLong_AsUnsignedLongLong(o);", "    if (x == (unsigned long long)-1 && PyErr_Occurred()) return -1;"]
                    if t.name != "u64":
                        L += [f"    if (x > {hi}ULL) {{ PyErr_SetString(PyExc_OverflowError, \"value out of range for {t.name}\"); return -1; }}"]
                L.append(f"    *out = ({ct})x;")
        elif k == "enum":
            n = len(t.sym.variants)
            L += ["    long x = PyLong_AsLong(o);", "    if (x == -1 && PyErr_Occurred()) return -1;",
                  f"    if (x < 0 || x >= {n}) {{ PyErr_SetString(PyExc_ValueError, \"not a variant of {t.sym.name} (0 to {n - 1})\"); return -1; }}",
                  f"    *out = ({ct})x;"]
        elif k == "opt":
            f = self.conv_to(t.inner)
            L += ["    if (o == Py_None) { out->has = false; return 0; }",
                  f"    if ({f}(o, &out->v) < 0) return -1;", "    out->has = true;"]
        elif k == "arr":
            f = self.conv_to(t.inner)
            L += ["    PyObject *seq = PySequence_Fast(o, \"expected a sequence\");", "    if (!seq) return -1;",
                  f"    if (PySequence_Fast_GET_SIZE(seq) != {t.n}) {{ PyErr_SetString(PyExc_ValueError, \"expected exactly {t.n} items\"); Py_DECREF(seq); return -1; }}",
                  f"    for (int64_t i = 0; i < {t.n}; i++) {{",
                  f"        if ({f}(PySequence_Fast_GET_ITEM(seq, (Py_ssize_t)i), &out->v[i]) < 0) {{"]
            if self.ck.has_glue(t.inner):
                L.append(f"            for (int64_t j = 0; j < i; j++) {self.drop_stmt(t.inner, 'out->v[j]')}")
            L += ["            Py_DECREF(seq); return -1;", "        }", "    }", "    Py_DECREF(seq);"]
        elif k == "struct" and self.is_list(t):
            et = t.sym.targs[0]
            f = self.conv_to(et)
            ect = self.cg.ctype(et)
            items, ln = self.cg.fld("items"), self.cg.fld("len")
            L += ["    PyObject *seq = PySequence_Fast(o, \"expected a list\");", "    if (!seq) return -1;",
                  "    int64_t n = (int64_t)PySequence_Fast_GET_SIZE(seq);",
                  f"    {ect} *buf = ({ect} *)cb_alloc(n, (int64_t)sizeof({ect}), 0, \"python\");",
                  f"    {ct} tmp; memset(&tmp, 0, sizeof tmp);",
                  f"    tmp.{items}.ptr = buf; tmp.{items}.len = n; tmp.{ln} = 0;",
                  "    for (int64_t i = 0; i < n; i++) {",
                  f"        if ({f}(PySequence_Fast_GET_ITEM(seq, (Py_ssize_t)i), &buf[i]) < 0) {{",
                  f"            {self.drop_stmt(t, 'tmp') or 'cb_mem_free(buf);'}",
                  "            Py_DECREF(seq); return -1;",
                  "        }",
                  f"        tmp.{ln} = i + 1;",
                  "    }",
                  "    Py_DECREF(seq);",
                  "    *out = tmp;"]
        elif k == "struct":
            self.stub_types[t.sym.full] = t.sym
            L.append(f"    {ct} tmp; memset(&tmp, 0, sizeof tmp);")
            done = []
            for fname, ft, _ in t.sym.fields:
                f = self.conv_to(ft)
                fc = self.cg.fld(fname)
                L += [f"    {{ PyObject *x = PyDict_Check(o) ? PyDict_GetItemString(o, {c_str(fname)}) : NULL;",
                      "      int fresh = 0;",
                      f"      if (!x && !PyDict_Check(o)) {{ x = PyObject_GetAttrString(o, {c_str(fname)}); fresh = 1; }}",
                      f"      if (!x) {{ if (!PyErr_Occurred()) PyErr_SetString(PyExc_KeyError, {c_str(fname)});"]
                for dn in done:
                    L.append("        " + dn)
                L += ["        return -1; }",
                      f"      int rc = {f}(x, &tmp.{fc});",
                      "      if (fresh) Py_DECREF(x);",
                      "      if (rc < 0) {"]
                for dn in done:
                    L.append("        " + dn)
                L += ["        return -1; } }"]
                d = self.drop_stmt(ft, f"tmp.{fc}")
                if d:
                    done.append(d)
            L.append("    *out = tmp;")
        else:
            raise Unsupported(f"no Python form for {t}")
        L += ["    return 0;", "}"]
        self.bodies.append("\n".join(L))
        return name

    # ---------------------------------------------------------------- functions
    def wrap_fn(self, idx, sym):
        cg = self.cg
        for p, t in zip(sym.node.params, sym.params):
            try:
                self.check(t)
            except Unsupported as e:
                raise Unsupported(f"parameter `{p.name}`: {e}")
        try:
            self.check(sym.ret, top_ret=True)
        except Unsupported as e:
            raise Unsupported(f"result: {e}")
        params = sym.node.params
        defaults = []
        for p, t in zip(params, sym.params):
            d = getattr(p, "default", None)
            if d is None:
                defaults.append(None)
                continue
            if d.kind == "Ident" and d.name == "null" and t.kind == "opt":
                defaults.append("Py_None")
            elif d.kind == "Int":
                defaults.append(f"PyLong_FromString({c_str(str(d.value))}, NULL, 10)")
            elif d.kind == "Float":
                defaults.append(f"PyFloat_FromDouble({repr(float(d.value))})")
            elif d.kind == "Bool":
                defaults.append("Py_True" if d.value else "Py_False")
            elif d.kind == "Str":
                v = d.value if isinstance(d.value, str) else d.value.decode("utf-8", "replace")
                defaults.append(f"PyUnicode_FromString({c_str(v)})")
            else:
                raise Unsupported(f"parameter `{p.name}` has a default value that is not a literal")
        wname = f"py_fn_{idx}"
        ret = sym.ret
        L = [f"static PyObject *{wname}(PyObject *self, PyObject *args, PyObject *kw) {{", "    (void)self;"]
        names = ", ".join(c_str(p.name) for p in params)
        L.append(f"    static char *kwlist[] = {{ {names + ', ' if params else ''}NULL }};")
        for i in range(len(params)):
            L.append(f"    PyObject *o{i} = NULL;")
        first_opt = next((i for i, d in enumerate(defaults) if d is not None), len(params))
        fmt = "O" * first_opt + ("|" + "O" * (len(params) - first_opt) if first_opt < len(params) else "")
        refs = "".join(f", &o{i}" for i in range(len(params)))
        if params:
            L.append(f"    if (!PyArg_ParseTupleAndKeywords(args, kw, {c_str(fmt)}, kwlist{refs})) return NULL;")
        else:
            L.append("    if (!PyArg_ParseTupleAndKeywords(args, kw, \"\", kwlist)) return NULL;")
        L.append("    int made = 0; (void)made;")
        for i, (p, t) in enumerate(zip(params, sym.params)):
            if defaults[i] is not None:
                L.append(f"    PyObject *own{i} = NULL;")
                L.append(f"    if (!o{i}) {{ o{i} = own{i} = {defaults[i]}; if (!o{i}) return NULL; if (own{i} == Py_None || own{i} == Py_True || own{i} == Py_False) {{ Py_INCREF(o{i}); }} }}")
        for i, t in enumerate(sym.params):
            L.append(f"    {cg.ctype(t)} a{i};")

        def cleanup(upto):
            out = []
            for j in range(upto):
                d = self.drop_stmt(sym.params[j], f"a{j}")
                if d:
                    out.append(d)
                if defaults[j] is not None:
                    out.append(f"Py_XDECREF(own{j});")
            for j in range(upto, len(params)):
                if defaults[j] is not None:
                    out.append(f"Py_XDECREF(own{j});")
            return " ".join(out)

        for i, t in enumerate(sym.params):
            f = self.conv_to(t)
            L.append(f"    if ({f}(o{i}, &a{i}) < 0) {{ {cleanup(i)} return NULL; }}")
        args = ", ".join(f"a{i}" for i in range(len(params)))
        rc = cg.ctype(ret) if ret != VOID else None
        decs = " ".join(f"Py_XDECREF(own{i});" for i in range(len(params)) if defaults[i] is not None)
        L += ["    jmp_buf jb; jmp_buf *prev = cb_py_jmp; cb_py_jmp = &jb;",
              f"    if (setjmp(jb)) {{ cb_py_jmp = prev; {decs} PyErr_SetString(py_panic_exc, cb_py_msg); return NULL; }}"]
        if rc:
            L.append(f"    {rc} rv = {sym.cname}({args});")
        else:
            L.append(f"    {sym.cname}({args});")
        L.append(f"    cb_py_jmp = prev; {decs}")
        if ret == VOID:
            L.append("    Py_RETURN_NONE;")
        elif ret.kind == "eu":
            L.append("    if (rv.err) { PyErr_SetString(py_error_exc, cb_err_names[rv.err]); return NULL; }")
            if ret.inner == VOID:
                L.append("    Py_RETURN_NONE;")
            else:
                f = self.conv_from(ret.inner)
                L.append(f"    PyObject *res = {f}(rv.val);")
                d = self.drop_stmt(ret.inner, "rv.val")
                if d:
                    L.append(f"    {d}")
                L.append("    return res;")
        else:
            f = self.conv_from(ret)
            L.append(f"    PyObject *res = {f}(rv);")
            d = self.drop_stmt(ret, "rv")
            if d:
                L.append(f"    {d}")
            L.append("    return res;")
        L.append("}")
        self.bodies.append("\n".join(L))
        return wname

    # ---------------------------------------------------------------- the module
    def generate(self):
        cg, ck = self.cg, self.ck
        table, adds = [], []
        n = 0
        for sym in ck.fns:
            if sym.extern or sym.mod is not self.mod or not sym.pub or sym.is_method or sym.tparams or sym.base is not None:
                continue
            if getattr(sym, "closure", None) is not None or sym.name.startswith("_"):
                continue
            try:
                w = self.wrap_fn(n, sym)
            except Unsupported as e:
                self.notes.append((sym.name, str(e)))
                continue
            n += 1
            sig = ", ".join(f"{p.name}: {t}" for p, t in zip(sym.node.params, sym.params))
            doc = f"{sym.name}({sig}) -> {sym.ret}"
            table.append(f"    {{ {c_str(sym.name)}, (PyCFunction)(void (*)(void)){w}, METH_VARARGS | METH_KEYWORDS, {c_str(doc)} }},")
            self.exposed.append((sym.name, sym))
        for sym in ck.consts:
            if sym.extern or sym.mod is not self.mod or not sym.pub or sym.ty is None:
                continue
            t = sym.ty
            if t.kind == "prim" and t != VOID:
                f = self.conv_from(t)
                adds.append(f"    {{ PyObject *c = {f}({sym.cname}); if (!c || PyModule_AddObjectRef(m, {c_str(sym.name)}, c) < 0) {{ Py_XDECREF(c); Py_DECREF(m); return NULL; }} Py_DECREF(c); }}")
                self.exposed.append((sym.name, sym))
        if not table and not adds:
            self.notes.append(("", "no `pub fn` of this file can be exposed to Python"))
        L = ["/* ---- Python extension module ---- */", "static PyObject *py_error_exc, *py_panic_exc;"]
        L += self.protos
        L += self.bodies
        L.append("static PyMethodDef py_methods[] = {")
        L += table
        L.append("    { NULL, NULL, 0, NULL }\n};")
        L.append(f"static struct PyModuleDef py_moddef = {{ PyModuleDef_HEAD_INIT, {c_str(self.modname)}, NULL, -1, py_methods }};")
        L.append(f"PyMODINIT_FUNC PyInit_{self.modname}(void) {{")
        L.append("#ifdef CB_GC\n    CB_GC_INIT();\n#endif")
        L.append("    PyObject *m = PyModule_Create(&py_moddef);")
        L.append("    if (!m) return NULL;")
        L.append(f"    py_error_exc = PyErr_NewException({c_str(self.modname + '.CrumbsError')}, NULL, NULL);")
        L.append(f"    py_panic_exc = PyErr_NewException({c_str(self.modname + '.Panic')}, PyExc_RuntimeError, NULL);")
        L.append("    if (!py_error_exc || !py_panic_exc) { Py_DECREF(m); return NULL; }")
        L.append("    if (PyModule_AddObjectRef(m, \"CrumbsError\", py_error_exc) < 0 || PyModule_AddObjectRef(m, \"Panic\", py_panic_exc) < 0) { Py_DECREF(m); return NULL; }")
        L += adds
        L.append("    return m;")
        L.append("}")
        return "\n".join(L) + "\n"

    # ---------------------------------------------------------------- the .pyi stub
    def py_type(self, t):
        k = t.kind
        if k == "prim":
            if t == BOOL:
                return "bool"
            if t.is_float:
                return "float"
            if t == VOID:
                return "None"
            if t == STRING or t.name == "str":
                return "str"
            return "int"
        if k == "enum":
            return "int"
        if k == "opt":
            return self.py_type(t.inner) + " | None"
        if k in ("arr", "slice"):
            return f"list[{self.py_type(t.inner)}]"
        if k == "eu":
            return self.py_type(t.inner) if t.inner != VOID else "None"
        if k == "struct":
            if self.is_list(t):
                return f"list[{self.py_type(t.sym.targs[0])}]"
            return t.sym.name
        return "object"

    def stub(self):
        out = ["from typing import TypedDict", "", "class CrumbsError(Exception): ...", "class Panic(RuntimeError): ...", ""]
        for full, s in self.stub_types.items():
            out.append(f"class {s.name}(TypedDict):")
            for fname, ft, _ in s.fields:
                out.append(f"    {fname}: {self.py_type(ft)}")
            if not s.fields:
                out.append("    pass")
            out.append("")
        for name, sym in self.exposed:
            if sym.kind == "const":
                out.append(f"{name}: {self.py_type(sym.ty)}")
                continue
            ps = []
            for p, t in zip(sym.node.params, sym.params):
                s = f"{p.name}: {self.py_type(t)}"
                if getattr(p, "default", None) is not None:
                    s += " = ..."
                ps.append(s)
            out.append(f"def {name}({', '.join(ps)}) -> {self.py_type(sym.ret)}: ...")
        return "\n".join(out) + "\n"
