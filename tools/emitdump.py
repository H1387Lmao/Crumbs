#!/usr/bin/env python3
"""Prints the C that the Python compiler emits for FILE (the reference for selfhost/emitdump.cb)."""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
r = subprocess.run([sys.executable, os.path.join(HERE, "..", "crumbs.py"), "--legacy", "emit", sys.argv[1]], capture_output=True, text=True)
sys.stdout.write(r.stdout)
