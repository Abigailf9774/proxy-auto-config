"""Proxy Auto Config (PAC) file parser and evaluator.

This package parses a PAC file (JavaScript) and evaluates its logic using
a minimal, sandboxed JavaScript evaluator implemented in pure Python. The
primary entry point is :class:`PACFile`.
"""

from .core import PACFile, ProxyConfig

__all__ = ["PACFile", "ProxyConfig"]
