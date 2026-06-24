"""Root conftest for the public checkout.

The published directory is named ``apply-engine`` (hyphen) for the repo, but the
package and all test imports use ``apply_engine`` (underscore). A hyphen is not a
valid module name, so we register this directory under the importable package
name and put its parent on ``sys.path`` before test collection. In the original
(non-public) layout the directory itself is named ``apply_engine`` and this shim
is unnecessary; here it lets the suite run unmodified from the public folder.
"""
import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Make `import apply_engine` resolve to THIS directory, regardless of the
# directory's on-disk name.
if "apply_engine" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "apply_engine", _HERE / "__init__.py",
        submodule_search_locations=[str(_HERE)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_engine"] = module
    spec.loader.exec_module(module)
