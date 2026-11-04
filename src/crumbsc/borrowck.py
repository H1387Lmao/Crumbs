"""Borrow checker: runs on one type-checked function body (the AST already carries types, locals and move marks).

What it tracks. A *loan* is a borrow of a local variable (or a field/element of one):
  * `&place`            exclusive loan (taking `&` already needs a `let mut` binding, so it is a `&mut` in Rust terms)
  * `place.method()`    with plain `self`: shared loan; with `mut self`: exclusive loan (the checker inserts the `&`)
A pointer-holding expression *carries* the loans of whatever it was made from (a call result carries the loans of its
arguments, a struct literal those of its fields, `let p = &x;` makes `p` hold the loan on `x`).

Rules (non-lexical: a loan lasts until the last use of the variable that holds it):
  1. while a loan on `x` is live, `x` cannot be written, moved or borrowed exclusively; if the loan is exclusive, `x` cannot be read either
  2. one call cannot take two overlapping borrows of the same place when one of them is exclusive
  3. a loan cannot outlive its owner: no returning `&local`, and a pointer stored in a longer-lived variable is an error if
     it is still used after the owner's scope ended
Raw pointers that come out of `alloc`, from a pointer parameter, or through `*p` carry no loans: they stay the unchecked escape hatch.
"""
from .parser import N

SKIP = {"span", "ty", "local", "sym", "handler", "orig_args", "eu_ty", "opty", "elem", "region_local", "name_span", "tyn", "targ", "targs"}


class Loan:
    __slots__ = ("owner", "path", "excl", "span", "via_own")

    def __init__(self, owner, path, excl, span, via_own=False):
        self.owner, self.path, self.excl, self.span, self.via_own = owner, path, excl, span, via_own


def overlap(a, b):
    n = min(len(a), len(b))
    return a[:n] == b[:n]


class BorrowCk:
    def __init__(self, ck):
        self.ck = ck
        self.d = ck.d

    # ------------------------------------------------------------------ entry
    def run(self, body, params):
        self.last = {}          # Local -> last tick at which it is used
        self.decl = {}          # Local -> tick of its declaration
        self.loops = []         # (start tick, end tick)
        for p in params:
            self.decl[p] = 0
        self.phase = 1
        self.tick = 0
        self.walk(body)
        for loc, uses in list(self.last.items()):
            for s, e in self.loops:
                if self.decl.get(loc, 0) < s and s <= uses <= e:
                    self.last[loc] = max(self.last[loc], e)       # used inside a loop: live to the end of it
        self.phase = 2
        self.tick = 0
        self.holds = {}         # Local -> [Loan] held by that variable
        self.moved = set()
        self.walk(body)

    # ------------------------------------------------------------------ helpers
    def kids(self, n):
        out = []
        for k, v in n.__dict__.items():
            if k in SKIP:
                continue
            for c in (v if isinstance(v, (list, tuple)) else [v]):
                if isinstance(c, N):
                    out.append(c)
                elif isinstance(c, tuple):
                    out.extend(x for x in c if isinstance(x, N))
        return out

    def holds_ptr(self, t):
        return t is not None and not getattr(t, "poison", False) and self.ck.holds_ptr(t)

    def live_after(self, h):
        return self.last.get(h, 0) > self.tick

    def place_root(self, e):
        """(local, path) when place `e` lives inside a local's own storage (not behind a pointer), else None."""
        path = []
        while True:
            k = e.kind
            if k == "Ident":
                loc = getattr(e, "local", None)
                return (loc, tuple(reversed(path))) if loc is not None and not getattr(loc, "region_handle", False) else None
            if k == "Field":
                if getattr(e, "mode", None) != "field":
                    return None
                path.append(e.name)
                e = e.obj
                continue
            if k == "Index":
                bt = getattr(e.obj, "ty", None)
                if bt is None or bt.kind != "arr":
                    return None
                path.append("[]")
                e = e.obj
                continue
            return None

    def name_of(self, loc):
        return loc.name

    # ------------------------------------------------------------------ access checks
    def access(self, loc, path, kind, span):
        """`kind`: read | write | move | excl (an exclusive borrow) | shared (a shared borrow)."""
        if self.phase != 2:
            return
        for h, loans in self.holds.items():
            if h is loc or not self.live_after(h):
                continue
            for ln in loans:
                if ln.owner is not loc or not overlap(ln.path, path):
                    continue
                if kind in ("read", "shared") and not ln.excl:
                    continue
                if kind == "move" and ln.via_own:
                    continue                         # the pointer is into the heap block, which does not move with its owner
                self.conflict(loc, kind, ln, h, span)
                return

    def conflict(self, loc, kind, ln, holder, span):
        what = {"read": "use", "write": "assign to", "move": "move out of", "excl": "borrow mutably", "shared": "borrow"}[kind]
        state = "mutably borrowed" if ln.excl else "borrowed"
        self.d.error(span, f"cannot {what} `{loc.name}` because it is {state}",
                     label=f"`{loc.name}` is {state} by `{holder.name}` (line {ln.span.line}), which is used again later",
                     help=f"finish using `{holder.name}` before this, or make a copy of what you need")

    # ------------------------------------------------------------------ the walk
    def walk(self, n):
        """Evaluate node `n`; returns the list of loans its value carries."""
        m = getattr(self, "w_" + n.kind, None)
        if m is not None:
            return m(n)
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        return carried if self.holds_ptr(getattr(n, "ty", None)) else []

    def w_Ident(self, n):
        loc = getattr(n, "local", None)
        if loc is None:
            return []
        self.tick += 1
        if self.phase == 1:
            self.last[loc] = self.tick
            self.decl.setdefault(loc, self.tick)
            return []
        if getattr(n, "move", False):
            self.access(loc, (), "move", n.span)
            self.moved.add(loc)
        else:
            self.access(loc, (), "read", n.span)
        return list(self.holds.get(loc, [])) if self.holds_ptr(loc.ty) else []

    def w_Field(self, n):
        return self.w_place_read(n)

    def w_Index(self, n):
        return self.w_place_read(n)

    def w_place_read(self, n):
        root = self.place_root(n)
        if root is not None:
            loc, path = root
            self.walk_index_parts(n)
            self.tick += 1
            if self.phase == 1:
                self.last[loc] = self.tick
                self.decl.setdefault(loc, self.tick)
                return []
            self.access(loc, path, "move" if getattr(n, "move", False) else "read", n.span)
            return list(self.holds.get(loc, [])) if self.holds_ptr(getattr(n, "ty", None)) else []
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        return carried if self.holds_ptr(getattr(n, "ty", None)) else []

    def base_ident(self, n):
        while n.kind in ("Field", "Index"):
            n = n.obj
        return n

    def is_chain_part(self, top, c):
        x = top
        while x.kind in ("Field", "Index"):
            if x.obj is c:
                return True
            x = x.obj
        return c is x

    def w_Unary(self, n):
        if n.op == "&":
            root = self.place_root(n.expr)
            if root is not None:
                loc, path = root
                excl = not getattr(n, "auto_ref", False)
                self.walk_index_parts(n.expr)
                self.tick += 1
                if self.phase == 1:
                    self.last[loc] = self.tick
                    self.decl.setdefault(loc, self.tick)
                    return []
                self.access(loc, path, "excl" if excl else "shared", n.span)
                return [Loan(loc, path, excl, n.span, via_own=self.is_own(loc.ty))]
        carried = []
        for c in self.kids(n):
            carried += self.walk(c)
        return carried if self.holds_ptr(getattr(n, "ty", None)) else []

    def is_own(self, t):
        return t.kind == "struct" and t.sym.name.startswith("Own") and "own" in t.sym.full

    def is_checked(self, t):
        """`std.own` handles that check themselves when used (`Ref`, `Weak`): they carry no loans."""
        return t is not None and t.kind == "struct" and t.sym.name.startswith(("Ref", "Weak")) and "own" in t.sym.full

    def walk_index_parts(self, e):
        while e.kind in ("Field", "Index"):
            if e.kind == "Index":
                self.walk(e.idx)
            e = e.obj

    def w_Call(self, n):
        c = n.callee
        carried_args = []
        skip_callee = getattr(n, "method_recv", False) or c.kind != "Ident" or getattr(c, "local", None) is None
        per_arg = []
        if not skip_callee:
            self.walk(c)
        for a in n.args:
            per_arg.append(self.walk(a))
        if self.is_checked(getattr(n, "ty", None)):
            return []
        sym = getattr(n, "sym", None)
        if self.phase == 2 and not getattr(sym, "extern", False):      # a C function takes raw pointers: aliasing is its business
            for i in range(len(per_arg)):
                for j in range(i + 1, len(per_arg)):
                    for l1 in per_arg[i]:
                        for l2 in per_arg[j]:
                            if l1.owner is l2.owner and overlap(l1.path, l2.path) and (l1.excl or l2.excl):
                                self.d.error(l2.span, f"cannot borrow `{l1.owner.name}` twice in one call",
                                             label=f"`{l1.owner.name}` is also borrowed at line {l1.span.line} in this call",
                                             help="borrow it once, or borrow different fields")
        for p in per_arg:
            carried_args += p
        return carried_args if self.holds_ptr(getattr(n, "ty", None)) else []

    def w_Assign(self, n):
        t = n.target
        carried = self.walk(n.value)
        root = self.place_root(t)
        if root is not None:
            loc, path = root
            self.walk_index_parts(t)
            self.tick += 1
            if self.phase == 1:
                self.last[loc] = self.tick
                self.decl.setdefault(loc, self.tick)
                return []
            if n.op == "=":
                self.access(loc, path, "write", t.span)
            else:
                self.access(loc, path, "write", t.span)
            if carried and self.holds_ptr(loc.ty):
                self.holds.setdefault(loc, []).extend(carried)
            return []
        # a store behind a pointer, or into a pointer-held place: only the pieces are evaluated
        for c in self.kids(t):
            self.walk(c)
        return []

    def w_IncDec(self, n):
        root = self.place_root(n.expr)
        if root is not None:
            loc, path = root
            self.walk_index_parts(n.expr)
            self.tick += 1
            if self.phase == 1:
                self.last[loc] = self.tick
                self.decl.setdefault(loc, self.tick)
                return []
            self.access(loc, path, "write", n.expr.span)
            return []
        self.walk(n.expr)
        return []

    def w_Let(self, n):
        carried = self.walk(n.init) if getattr(n, "init", None) is not None else []
        loc = getattr(n, "local", None)
        if loc is not None:
            if self.phase == 1:
                self.decl[loc] = self.tick
                self.last.setdefault(loc, self.tick)
            elif carried and self.holds_ptr(loc.ty):
                self.holds[loc] = list(carried)
        return []

    def w_Block(self, n):
        start = self.tick
        owners = []
        for st in n.stmts if hasattr(n, "stmts") else self.kids(n):
            self.walk(st)
            if st.kind == "Let" and getattr(st, "local", None) is not None:
                owners.append(st.local)
        if self.phase == 2:
            for x in owners:
                for h, loans in self.holds.items():
                    if h is x or self.decl.get(h, 0) > start or not self.live_after(h):
                        continue
                    for ln in loans:
                        if ln.owner is x:
                            self.outlive(ln, x, h)
                            break
        return []

    def outlive(self, ln, owner, holder):
        if ln.via_own:
            msg = f"pointer into the owned value `{owner.name}` would outlive it if stored here"
            label = f"`{owner.name}` is freed when its scope ends, so this pointer would dangle"
        else:
            msg = f"`{owner.name}` does not live long enough"
            label = f"`{holder.name}` borrows `{owner.name}` here but is used after `{owner.name}` goes out of scope"
        self.d.error(ln.span, msg, label=label, help="move the owner out instead, or keep the borrow inside this scope")

    def w_Return(self, n):
        if getattr(n, "expr", None) is None:
            return []
        carried = self.walk(n.expr)
        if self.phase == 2:
            for ln in carried:
                if ln.owner in self.moved:
                    continue
                if ln.via_own:
                    self.d.error(ln.span, f"pointer into the owned value `{ln.owner.name}` cannot be returned",
                                 label=f"`{ln.owner.name}` is freed when its scope ends, so this pointer would dangle",
                                 help="move the owner out instead (return it or store it), or keep a checked `Ref` with `.ref()`")
                else:
                    self.d.error(ln.span, f"cannot return a reference to the local `{ln.owner.name}`",
                                 label=f"`{ln.owner.name}` is destroyed when the function returns, so this would dangle",
                                 help="return the value itself, or allocate it with `alloc` / `own.new`")
                break
        return []

    def w_While(self, n):
        return self.loop(n)

    def w_For(self, n):
        return self.loop(n)

    def w_Loop(self, n):
        return self.loop(n)

    def loop(self, n):
        start = self.tick
        loc = getattr(n, "local", None)
        if loc is not None and self.phase == 1:
            self.decl[loc] = self.tick
        for c in self.kids(n):
            self.walk(c)
        if self.phase == 1:
            self.loops.append((start + 1, self.tick))
        return []

    def w_Fn(self, n):
        return []
