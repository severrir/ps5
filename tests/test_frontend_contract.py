"""Static checks on the frontend.

A hand-written single-page UI of this size has a few failure modes that are silent
in Python but fatal in the browser, and tedious to spot on a TV across the room:

* an id referenced by ``getElementById`` that no longer exists in the markup
* an icon ``<use xlink:href="#i-x">`` with no matching symbol
* a stylesheet or script path that does not resolve
* an inline ``<script>`` or ``style="..."`` attribute, which the CSP blocks outright
* a ``data-i18n`` key missing from the string table
* modern syntax the console's WebKit may not parse

These run in milliseconds and catch all of it before the console does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "index.html"
STATIC = ROOT / "static"
JS_DIR = STATIC / "js"
CSS_DIR = STATIC / "css"

# XML namespace URIs are identifiers every inline SVG must carry; nothing fetches
# them. The runtime audit allowlists them for the same reason.
NAMESPACE_URIS = ("www.w3.org/2000/svg", "www.w3.org/1999/xlink", "www.w3.org/1999/xhtml")


def strip_comments(source: str) -> str:
    """Remove ``/* */`` and ``//`` comments.

    Most checks here look for a construct in the *code*. Without this they also
    match the comment explaining why that construct is banned -- a false positive
    every time a rule is documented next to its enforcement.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", source)


@pytest.fixture(scope="module")
def html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js_sources() -> dict:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(JS_DIR.glob("*.js"))}


@pytest.fixture(scope="module")
def css_sources() -> dict:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(CSS_DIR.glob("*.css"))}


# --------------------------------------------------------------- asset paths -- #


def test_every_referenced_asset_exists(html):
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "the shell should reference its own assets"
    missing = [ref for ref in refs if not (ROOT / ref.lstrip("/")).is_file()]
    assert not missing, f"referenced but not on disk: {missing}"


def test_every_script_file_is_referenced(html):
    referenced = set(re.findall(r'src="/static/js/([^"]+)"', html))
    on_disk = {path.name for path in JS_DIR.glob("*.js")}
    assert not (on_disk - referenced), f"unreferenced script files: {sorted(on_disk - referenced)}"
    assert not (referenced - on_disk), f"referenced but missing: {sorted(referenced - on_disk)}"


def test_every_stylesheet_is_referenced(html):
    referenced = set(re.findall(r'href="/static/css/([^"]+)"', html))
    on_disk = {path.name for path in CSS_DIR.glob("*.css")}
    assert referenced == on_disk


# ------------------------------------------------------------------- icons --- #


def icons_used(html: str, js_sources: dict) -> set:
    """Icon names referenced from markup or from any JS helper that takes one."""
    used = set(re.findall(r'xlink:href="#i-([a-z0-9-]+)"', html))
    for source in js_sources.values():
        body = strip_comments(source)
        used |= set(re.findall(r'icon\("([a-z0-9-]+)"', body))
        used |= set(re.findall(r'actionButton\("([a-z0-9-]+)"', body))
    return used


def icons_defined(html: str) -> set:
    return set(re.findall(r'<g id="i-([a-z0-9-]+)"', html))


def test_every_icon_reference_has_a_symbol(html, js_sources):
    defined = icons_defined(html)
    assert defined, "the inline sprite should define symbols"
    missing = icons_used(html, js_sources) - defined
    assert not missing, f"icons used with no symbol defined: {sorted(missing)}"


def test_no_unused_icons(html, js_sources):
    """Keeps the inline sprite from carrying dead weight in every page load."""
    unused = icons_defined(html) - icons_used(html, js_sources)
    assert not unused, f"icons defined but never used: {sorted(unused)}"


# ---------------------------------------------------------------- element ids - #

# Ids created at runtime rather than declared in the template.
DYNAMIC_IDS = {"modal-field-value"}


def test_every_element_id_used_by_js_exists(html, js_sources):
    declared = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html)) | DYNAMIC_IDS

    missing = {}
    for name, source in js_sources.items():
        used = set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', strip_comments(source)))
        gaps = used - declared
        if gaps:
            missing[name] = sorted(gaps)
    assert not missing, f"JS looks up ids that the markup does not define: {missing}"


def sections_from_router(js_sources: dict) -> list:
    listed = re.search(r"var SECTIONS = \[([^\]]+)\]", js_sources["app.js"])
    assert listed, "app.js should declare its section list"
    return re.findall(r'"([a-z]+)"', listed.group(1))


def test_section_ids_match_the_router(html, js_sources):
    sections = sections_from_router(js_sources)
    assert len(sections) >= 5
    for section in sections:
        assert f'id="section-{section}"' in html, f"no markup for section '{section}'"
        assert f'data-section="{section}"' in html, f"no rail button for section '{section}'"


def test_every_rail_button_has_a_section(html, js_sources):
    buttons = set(re.findall(r'data-section="([a-z]+)"', html))
    assert buttons == set(sections_from_router(js_sources))


def test_every_section_has_a_focusable_element(html, js_sources):
    """A section with nothing focusable is a dead end for the D-pad."""
    for section in sections_from_router(js_sources):
        match = re.search(
            r'id="section-' + section + r'"(.*?)(?=<section|</main>)', html, re.DOTALL
        )
        assert match, f"could not isolate section '{section}'"
        assert "data-focusable" in match.group(1), f"section '{section}' has nothing focusable"


# ------------------------------------------------------------------- CSP ------ #


def test_no_inline_script(html):
    """script-src 'self' blocks inline script entirely."""
    inline = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>(.*?)</script>", html, re.DOTALL)
    assert not [block for block in inline if block.strip()], "inline <script> is blocked by the CSP"


def test_no_style_attributes_in_markup(html):
    """style-src 'self' blocks style attributes as well as <style> elements."""
    assert not re.search(r"\sstyle=", html), "use a CSS class or element.style instead"


def test_no_style_elements(html):
    assert "<style" not in html


def test_no_remote_references_anywhere(html, js_sources, css_sources):
    """Mirrors the runtime air-gap audit, as a unit test."""
    offenders = []
    sources = list(js_sources.items()) + list(css_sources.items()) + [("index.html", html)]
    for name, source in sources:
        body = source if name == "index.html" else strip_comments(source)
        for line_number, line in enumerate(body.splitlines(), start=1):
            for match in re.finditer(r"https?://[^\s\"'`<>()\\]+", line):
                url = match.group(0)
                if any(namespace in url for namespace in NAMESPACE_URIS):
                    continue
                if re.search(r"(src=|href=|@import|url\(|fetch\(|new Image\()", line):
                    offenders.append(f"{name}:{line_number} {url}")
    assert not offenders, f"remote references found: {offenders}"


def test_no_external_font_loading(css_sources):
    for name, source in css_sources.items():
        assert "@font-face" not in source, f"{name} loads a font file"
        assert "fonts.googleapis" not in source


# ------------------------------------------------------------------ i18n ------ #


def english_keys(js_sources: dict) -> set:
    block = re.search(r"en:\s*\{(.*?)\n    \},", js_sources["i18n.js"], re.DOTALL)
    assert block, "the English table should be findable"
    return set(re.findall(r'"([a-z0-9.]+)":', block.group(1)))


def test_every_i18n_key_exists_in_english(html, js_sources):
    defined = english_keys(js_sources)

    used = set(re.findall(r'data-i18n="([a-z0-9.]+)"', html))
    for source in js_sources.values():
        body = strip_comments(source)
        used |= set(re.findall(r'i18n\.t\("([a-z0-9.]+)"', body))
        # The local alias `var t = window.HUB.i18n.t`. The lookbehind matters:
        # without it, `new Event("input")` matches as t("input").
        used |= set(re.findall(r'(?<![\w.$])t\("([a-z0-9.]+)"', body))

    # Keys composed at runtime -- t("nav." + section) -- cannot be checked here.
    used = {key for key in used if not key.endswith(".")}

    missing = used - defined
    assert not missing, f"i18n keys with no English string: {sorted(missing)}"


def test_runtime_composed_i18n_prefixes_are_fully_populated(js_sources):
    """The keys built by concatenation still have to exist for every value used.

    ``t("nav." + section)`` is unverifiable directly, so instead check that every
    prefix the code composes has an entry for each section and transfer state.
    """
    defined = english_keys(js_sources)
    for section in sections_from_router(js_sources):
        assert "nav." + section in defined, f"no nav label for '{section}'"
        assert "sub." + section in defined, f"no subtitle for '{section}'"


def test_translations_do_not_invent_keys(js_sources):
    english = english_keys(js_sources)
    for code in ("ka", "ru"):
        block = re.search(code + r":\s*\{(.*?)\n    \}", js_sources["i18n.js"], re.DOTALL)
        assert block, f"missing {code} table"
        keys = set(re.findall(r'"([a-z0-9.]+)":', block.group(1)))
        assert keys <= english, f"{code} defines keys absent from English: {sorted(keys - english)}"


def test_every_transfer_state_has_a_label(js_sources):
    """The queue renders state labels through i18n; every state needs one."""
    states = re.findall(r'"(queued|starting|installing|paused|retrying|done|failed|cancelled)"', js_sources["transfers.js"])
    assert states, "transfers.js should reference states"
    defined = english_keys(js_sources)
    for state in set(states):
        assert "state." + state in defined, f"no label for state '{state}'"


# ------------------------------------------- console WebKit compatibility ----- #


def test_no_es_modules(html):
    assert 'type="module"' not in html, "classic scripts only, for older WebKit"


def test_no_modern_syntax_the_console_may_not_parse(js_sources):
    """ES2017 baseline: the PS5 browser is WebKit, and not always a recent one."""
    patterns = {
        "optional chaining (?.)": r"[A-Za-z_$\])]\?\.",
        "nullish coalescing (??)": r"\?\?",
        "private class fields": r"^\s*#[A-Za-z_]+\s*[=;]",
        "logical assignment": r"(?:\|\||&&|\?\?)=",
    }
    offenders = []
    for name, source in js_sources.items():
        body = strip_comments(source)
        for label, pattern in patterns.items():
            for line_number, line in enumerate(body.splitlines(), start=1):
                if re.search(pattern, line):
                    offenders.append(f"{name}:{line_number} {label}")
    assert not offenders, f"syntax that may not parse on the console: {offenders}"


def test_no_css_features_older_webkit_lacks(css_sources):
    for name, source in css_sources.items():
        body = strip_comments(source)
        assert ":has(" not in body, f"{name} uses :has()"
        assert "aspect-ratio" not in body, f"{name} uses aspect-ratio; use the padding ratio trick"
        assert "@layer" not in body, f"{name} uses @layer"


def test_clamp_usage_is_feature_guarded(css_sources):
    """clamp() is fine on recent WebKit but must have a flat fallback first."""
    tokens = css_sources["tokens.css"]
    assert "@supports (font-size: clamp(" in tokens
    base_block = tokens.split("@supports")[0]
    for token in ("--fs-micro", "--fs-small", "--fs-body", "--fs-lead", "--fs-title", "--fs-display"):
        assert token in base_block, f"{token} has no non-clamp fallback"


def test_portrait_cards_use_the_padding_ratio_trick(css_sources):
    """3:4 box art without aspect-ratio, which older WebKit lacks."""
    assert "padding-top: 133.33%" in css_sources["components.css"]


# ------------------------------------------------------------- JS structure --- #


def test_every_module_registers_itself_on_the_hub_namespace(js_sources):
    """No bundler, so the single global namespace is the only wiring there is."""
    for name, source in js_sources.items():
        assert "window.HUB" in source, f"{name} does not attach to window.HUB"
        assert '"use strict"' in source, f"{name} is not in strict mode"


def test_no_native_dialogs(js_sources):
    """alert/confirm/prompt block the whole page in the console's browser."""
    offenders = []
    for name, source in js_sources.items():
        for line_number, line in enumerate(strip_comments(source).splitlines(), start=1):
            if re.search(r"(?<![.\w])(?:alert|confirm|prompt)\s*\(", line):
                # modal.confirm / modal.prompt are ours, not the native ones.
                if re.search(r"(?:modal|window\.HUB)\.(?:confirm|prompt)", line):
                    continue
                offenders.append(f"{name}:{line_number} {line.strip()[:80]}")
    assert not offenders, f"native dialog calls found: {offenders}"


def test_api_layer_stays_same_origin(js_sources):
    """Paths are built by concatenation, so fragments are expected; a scheme and
    host are not, and would be the only way a call here could leave this origin."""
    api = strip_comments(js_sources["api.js"])
    assert "/api/" in api, "the api layer should declare its endpoints"
    assert not re.search(r"[\"'`]https?://", api), "the api layer must stay same-origin"


def test_every_api_helper_is_reachable(js_sources):
    """An api helper nothing calls is either dead code or a forgotten wiring."""
    api = strip_comments(js_sources["api.js"])
    helpers = set(re.findall(r"^    ([a-zA-Z]+): function", api, re.MULTILINE))
    # Internals and the generic verbs are called from inside api.js itself.
    helpers -= {"get", "post", "del", "ApiError"}

    # Whitespace is stripped because the call sites are formatted across lines:
    #   window.HUB.api
    #     .setFavorite(id, true)
    callers = "".join(strip_comments(src) for name, src in js_sources.items() if name != "api.js")
    callers = re.sub(r"\s+", "", callers)
    unused = {name for name in helpers if ("api." + name + "(") not in callers}
    assert not unused, f"api helpers nothing calls: {sorted(unused)}"
