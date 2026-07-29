"""Compatibility package for running teleimager from a repository checkout."""

from pathlib import Path

_src_pkg = Path(__file__).resolve().parent / "src" / "teleimager"
if _src_pkg.exists():
    __path__.append(str(_src_pkg))
