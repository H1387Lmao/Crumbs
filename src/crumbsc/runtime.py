"""The C prelude emitted at the top of every generated file; it lives in runtime.c so editors highlight it."""
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.c"), encoding="utf-8") as _f:
    RUNTIME = _f.read()
