"""Step 6: static checks of the single-page UI (static/index.html). ARCHITECTURE 13.7, items 1-3.

No browser: the page is read as text. The click-through (items 4-12) is done by QA in Chrome.
"""
import re
from pathlib import Path

import pytest
from werkzeug.exceptions import MethodNotAllowed, NotFound

from app import WORKSPACE_SCRIPTS, create_app
from jobs import JobManager
from store import Store

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
MAX_BYTES = 150 * 1024


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(blocks) == 1, "the page has exactly one inline script"
    return blocks[0]


@pytest.fixture(scope="module")
def all_scripts(script) -> str:
    """The inline script plus the SQL workspace scripts it loads (Step 8)."""
    return "\n".join([script] + [(PAGE.parent / n).read_text(encoding="utf-8") for n in WORKSPACE_SCRIPTS])


@pytest.fixture(scope="module")
def flask_app(tmp_path_factory):
    st = Store(tmp_path_factory.mktemp("s6"))
    return create_app(st, JobManager(st))


def api_calls(script: str) -> list[tuple[str, str]]:
    """(METHOD, path) of every `api('<METHOD>', '<literal or template>'...)` call, `${...}` replaced by `x`."""
    calls = []
    for method, tpl, lit in re.findall(r"api\('(GET|POST|DELETE)',\s*(?:`(/api/[^`]*)`|'(/api/[^']*)')", script):
        path = re.sub(r"\$\{[^}]*\}", "x", tpl or lit).split("?")[0]
        calls.append((method, path))
    return calls


# ---------------------------------------------------------------- 1. served, small, self-contained

def test_page_is_served_without_the_guard_header(flask_app, html):
    r = flask_app.test_client().get("/")
    assert r.status_code == 200
    assert r.get_data() == PAGE.read_bytes()   # bytes: a CRLF checkout (core.autocrlf) must still match
    assert "text/html" in r.content_type


def test_page_size_budget(html):
    assert len(html.encode("utf-8")) < MAX_BYTES


def test_no_external_resources(html):
    # the only script files are the app's own workspace scripts, by relative path (Step 8)
    srcs = re.findall(r"<script[^>]+src=[\"']([^\"']*)", html, re.I)
    assert srcs == ["ws/grid.js", "ws/console.js", "ws/explorer.js"], srcs
    assert set(srcs) <= {f"ws/{n}" for n in WORKSPACE_SCRIPTS}
    assert not re.search(r"<link[^>]+href=", html, re.I)
    assert not re.search(r"@import|url\(\s*['\"]?https?:", html, re.I)
    # the only absolute URL allowed is the SVG namespace
    urls = set(re.findall(r"https?://[^\s'\"<>)]+", html))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls


def test_light_and_dark_mode(html):
    assert "prefers-color-scheme: dark" in html
    assert ':root[data-theme="dark"]' in html


# ---------------------------------------------------------------- 2. every API call matches a route

def test_every_api_call_matches_a_flask_route(flask_app, all_scripts):
    calls = api_calls(all_scripts)
    assert len(calls) >= 25, f"expected the UI to use most of the API, found {len(calls)} calls"
    adapter = flask_app.url_map.bind("127.0.0.1")
    bad = []
    for method, path in calls:
        try:
            adapter.match(path, method=method)
        except (NotFound, MethodNotAllowed) as e:
            bad.append(f"{method} {path}: {type(e).__name__}")
    assert not bad, bad


def test_ui_covers_every_api_route(flask_app, all_scripts):
    """Each API rule is used by at least one call in the page or the workspace scripts."""
    adapter = flask_app.url_map.bind("127.0.0.1")
    used = set()
    for method, path in api_calls(all_scripts):
        endpoint, _ = adapter.match(path, method=method)
        used.add((endpoint, method))
    rules = {(r.endpoint, m) for r in flask_app.url_map.iter_rules() if r.rule.startswith("/api/")
             for m in r.methods - {"HEAD", "OPTIONS"}}
    assert not rules - used, sorted(rules - used)


def test_requests_carry_the_guard_header_and_go_through_api(script, all_scripts):
    assert "'X-PGBS': '1'" in script
    # the only fetch() is the one inside api()
    assert len(re.findall(r"\bfetch\(", all_scripts)) == 1
    assert "XMLHttpRequest" not in all_scripts


# ---------------------------------------------------------------- 3. no HTML injection, no browser dialogs

def test_no_html_parsing_of_variables(all_scripts):
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment"):
        assert sink not in all_scripts, f"{sink} is not allowed (decision 008 §2)"


def test_no_browser_dialogs_or_eval(all_scripts):
    for bad in (r"\balert\(", r"\bconfirm\(", r"\bprompt\(", r"\beval\(", r"new Function\("):
        assert not re.search(bad, all_scripts), bad


def test_text_goes_through_text_nodes(script):
    assert "document.createTextNode" in script
    assert "node.textContent = v" in script


def test_passwords_are_never_put_back_into_the_form(script):
    # the server form password field never receives a value; the saved state is shown as a placeholder
    m = re.search(r"password: el\('input', \{([^}]*)\}", script)
    assert m and "value" not in m.group(1)
    assert "has_password" in script
