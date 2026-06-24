import ast
import functools
import http.server
import inspect
import re
import threading
from pathlib import Path
import pytest

FIX = Path(__file__).parent / "fixtures"

# A test needs a real Chromium-family browser if its body — or any fixture it uses — drives a live
# page. The two entry points that open one are browser.launch_profile (direct page control) and
# orchestrator.apply_to_job (the real apply path, which calls launch_profile internally). Any test
# whose reachable source mentions either is browser-coupled and is marked `browser` so CI (which has
# no browser) can deselect it via `-m "not browser"`.
_BROWSER_TRIGGERS = ("launch_profile", "apply_to_job(")


def pytest_configure(config):
    # Opt-in marker for the slow, real-`claude -p` behavioural tests (test_ledger_prose_llm.py).
    # Registering it keeps `-m llm` deselection clean and silences the unknown-marker warning.
    config.addinivalue_line(
        "markers",
        "llm: slow test that shells out to the real Claude CLI; opt-in via `-m llm`.",
    )
    # Tests that drive a live browser. Deselected by default (addopts `-m "not browser"`) because the
    # offline CI image installs no Chromium/Chrome; run them explicitly with `-m browser` on a host
    # that has a browser.
    config.addinivalue_line(
        "markers",
        "browser: test drives a live Chromium-family browser (launch_profile / apply_to_job); "
        "needs a browser, deselected by default via `-m \"not browser\"`.",
    )


def _direct_trigger(src: str) -> bool:
    return any(trigger in src for trigger in _BROWSER_TRIGGERS)


# Cache, per test module, the names of MODULE-LEVEL helper functions whose body reaches a browser
# trigger (e.g. test_form_spec.py's `_spec`, which calls launch_profile). A test that calls one of
# these helpers is browser-coupled even though its own body never names launch_profile/apply_to_job.
_browser_helpers_by_module: dict = {}


def _browser_helpers(module) -> set:
    name = getattr(module, "__name__", None)
    if name in _browser_helpers_by_module:
        return _browser_helpers_by_module[name]
    helpers = set()
    try:
        src = inspect.getsource(module)
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test_"):
                seg = ast.get_source_segment(src, node) or ""
                if _direct_trigger(seg):
                    helpers.add(node.name)
    except (OSError, TypeError, SyntaxError):
        pass
    _browser_helpers_by_module[name] = helpers
    return helpers


def _reaches_browser(item) -> bool:
    """True if the test reaches a live browser via: its own body, a fixture it requests, or a
    module-level helper function it calls."""
    sources = []
    try:
        sources.append(inspect.getsource(item.function))
    except (OSError, TypeError):
        pass
    # Fixtures the test requests (covers fixture-based browser tests whose own body never names a
    # trigger but consume a fixture that does).
    finfo = getattr(item, "_fixtureinfo", None)
    if finfo is not None:
        for defs in getattr(finfo, "name2fixturedefs", {}).values():
            for fixturedef in defs:
                func = getattr(fixturedef, "func", None)
                if func is None:
                    continue
                try:
                    sources.append(inspect.getsource(func))
                except (OSError, TypeError):
                    pass
    blob = "\n".join(sources)
    if _direct_trigger(blob):
        return True
    # Module-level helper indirection (e.g. `_spec(...)` -> launch_profile).
    module = inspect.getmodule(item.function)
    helpers = _browser_helpers(module) if module is not None else set()
    return any(re.search(r"\b" + re.escape(h) + r"\(", blob) for h in helpers)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "browser" in item.keywords:
            continue
        if _reaches_browser(item):
            item.add_marker(pytest.mark.browser)


@pytest.fixture(autouse=True)
def _isolate_manifest(tmp_path_factory, monkeypatch):
    """Redirect the staged-application manifest to a throwaway dir so orchestrator
    tests never write to the live ARIA data hub.

    Also isolates the corrections ledger: regen_answer now mirrors every answer edit into
    corrections_log, which appends to a file at the career root. Without this, exercising the
    real regen_answer entrypoint in tests would pollute the production ledger. corrections_log
    lives one level up from this package; import it the same way regen_answer does."""
    from apply_engine import config
    sandbox = tmp_path_factory.mktemp("aria_data")
    monkeypatch.setattr(config, "ARIA_DATA", sandbox)
    try:
        import corrections_log
        monkeypatch.setattr(corrections_log, "LEDGER_PATH", sandbox / "corrections_ledger.jsonl")
    except Exception:
        pass


@pytest.fixture(scope="session")
def fixture_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX))
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
