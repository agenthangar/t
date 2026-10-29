"""One TOML API on supported hosts; private pinned fallback, never global pip."""
import importlib
from pathlib import Path
import sys
import types

try:
    import tomllib as parser
except ImportError:
    package = sys.modules.setdefault('_t_vendor', types.ModuleType('_t_vendor'))
    package.__path__ = [str(Path(__file__).with_name('vendor'))]
    parser = importlib.import_module('_t_vendor.tomli')

load = parser.load
loads = parser.loads
TOMLDecodeError = parser.TOMLDecodeError
