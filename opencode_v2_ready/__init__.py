"""opencode-v2-ready: which OpenCode plugins run on OpenCode 2.0, and does your opencode.json load?

Stdlib only. ``check`` reads your opencode.json(c) / .opencode/ directory and tells, per plugin, whether it
targets the V1 plugin API (``@opencode-ai/plugin``) or the V2 one (``@opencode/plugin``) using the live npm
registry. ``build`` scans every npm package tagged ``opencode-plugin`` and renders a static board.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "0.1.0"

REGISTRY = "https://registry.npmjs.org/"
USER_AGENT = "opencode-v2-ready/%s (+https://github.com/rafal-one/opencode-v2-ready; rafal.baranowski.iot@gmail.com)" % __version__
DONATE = "https://paypal.me/RafalBaranowski"
REPO = "https://github.com/rafal-one/opencode-v2-ready"
DEFAULT_BASE = "https://rafal-one.github.io/opencode-v2-ready"
MIGRATE_DOC = "https://opencode.ai/v2/docs/build/plugins/migrate-v1/"

V1_PKG = "@opencode-ai/plugin"
V2_PKG = "@opencode/plugin"
DEP_FIELDS = ("dependencies", "peerDependencies", "optionalDependencies", "devDependencies")
SEARCH_QUERIES = ("keywords:opencode-plugin", "keywords:opencode-plugins")

# status -> (text, shields colour, ready for V2?)
STATUS = {
    "ready": ("V2 ready", "brightgreen", True),
    "dual": ("V1 + V2", "green", True),
    "v1": ("V1 only", "red", False),
    "unclear": ("unclear", "lightgrey", False),
}
STATUS_ORDER = ("v1", "unclear", "dual", "ready")

# Markers searched in source or tarball text when no dependency on either plugin package is declared
# (bundled plugins). V2: the Plugin.define entrypoint and ctx.* domains. V1: V1 hook keys and the V1 package.
V2_MARKERS = (re.compile(r"@opencode/plugin"), re.compile(r"\bPlugin\s*\.\s*define\s*\("),
              re.compile(r"\bctx\s*\.\s*(?:tool|session|permission|shell)\s*\.\s*(?:hook|transform)\s*\("),
              re.compile(r"\bctx\s*\.\s*event\s*\.\s*subscribe\s*\("))
V1_MARKERS = (re.compile(r"@opencode-ai/plugin"),
              re.compile(r"""["'](?:tool\.execute\.(?:before|after)|chat\.(?:message|params|headers)|permission\.ask|shell\.env|"""
                         r"""experimental\.chat\.(?:system|messages)\.transform|experimental\.session\.compacting|tool\.definition)["']"""))
SOURCE_EXT = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx")

# V1 hook -> V2 API (closest equivalent), for the port page and tests
HOOK_MAP = [
    ("event", "ctx.event.subscribe()"), ("dispose", "cleanup function returned by setup"),
    ("config", "transforms on the affected domains"), ("tool", "ctx.tool.transform(...)"),
    ("auth", "ctx.integration.transform(...)"), ("provider", "ctx.provider.transform(...) and ctx.model.transform(...)"),
    ("chat.message", 'ctx.session.hook("prompt", ...)'), ("chat.params", 'ctx.session.hook("context", ...)'),
    ("chat.headers", 'ctx.session.hook("model.request", ...) or "http.request"'),
    ("permission.ask", 'ctx.permission.hook("evaluate", ...)'),
    ("command.execute.before", "ctx.command.transform(...) or the prompt hook"),
    ("tool.execute.before", 'ctx.tool.hook("execute.before", ...)'), ("tool.execute.after", 'ctx.tool.hook("execute.after", ...)'),
    ("shell.env", 'ctx.shell.hook("create.before", ...)'), ("tool.definition", "ctx.tool.transform(...)"),
    ("experimental.chat.system.transform", 'ctx.session.hook("context", ...), edit event.system'),
    ("experimental.chat.messages.transform", 'ctx.session.hook("context", ...), edit event.messages'),
    ("experimental.session.compacting", 'ctx.session.hook("compaction", ...)'),
]
ERRORS = ["default export must be an object with id", "{ id, setup }"]


# ---------------------------------------------------------------- classification

def dep_range(manifest: dict, pkg: str) -> Optional[str]:
    for field in DEP_FIELDS:
        v = (manifest.get(field) or {}).get(pkg)
        if v:
            return str(v)
    return None


def scan_text(text: str) -> Tuple[bool, bool]:
    """Return (v1_marker_found, v2_marker_found) for a piece of source text."""
    return (any(m.search(text) for m in V1_MARKERS), any(m.search(text) for m in V2_MARKERS))


def classify(manifest: dict, code: Optional[Tuple[bool, bool]] = None) -> dict:
    """Classify a package.json manifest. ``code`` is an optional (v1, v2) result of scanning the sources."""
    r1, r2 = dep_range(manifest, V1_PKG), dep_range(manifest, V2_PKG)
    evidence = "dependency"
    v1, v2 = r1 is not None, r2 is not None
    if not v1 and not v2:
        evidence = "code" if code and (code[0] or code[1]) else "none"
        if code:
            v1, v2 = code
    status = "dual" if v1 and v2 else "ready" if v2 else "v1" if v1 else "unclear"
    return {"status": status, "evidence": evidence, "v1_range": r1, "v2_range": r2}


def repo_slug(manifest: dict) -> Optional[str]:
    repo = manifest.get("repository")
    url = repo.get("url") if isinstance(repo, dict) else repo
    if not isinstance(url, str):
        return None
    m = re.search(r"(?:github\.com[:/]+|^github:)([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#].*)?$", url)
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


# ---------------------------------------------------------------- HTTP

def http_get(url: str, accept: str = "application/json", retries: int = 4, timeout: int = 60,
             headers: Optional[dict] = None) -> Optional[bytes]:
    """GET with backoff on 429/5xx. Returns None on 404."""
    delay = 2.0
    for attempt in range(retries + 1):
        hdr = {"User-Agent": USER_AGENT, "Accept": accept}
        hdr.update(headers or {})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(float(wait), 120) if wait and wait.isdigit() else delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None


def pkg_url(name: str, version: str = "latest") -> str:
    return REGISTRY + urllib.parse.quote(name, safe="@").replace("/", "%2F") + "/" + urllib.parse.quote(version, safe="")


def fetch_manifest(name: str, version: str = "latest") -> Optional[dict]:
    data = http_get(pkg_url(name, version))
    return json.loads(data) if data else None


def fetch_search(query: str, limit: int = 3000, pause: float = 1.0) -> List[dict]:
    out: List[dict] = []
    for off in range(0, limit, 250):
        data = http_get(REGISTRY + "-/v1/search?text=%s&size=250&from=%d" % (urllib.parse.quote(query), off))
        objs = json.loads(data).get("objects", []) if data else []
        out += objs
        if len(objs) < 250:
            break
        time.sleep(pause)
    return out


def scan_tarball(manifest: dict, max_bytes: int = 1_500_000, fetch: Optional[Callable[[str], Optional[bytes]]] = None
                 ) -> Optional[Tuple[bool, bool]]:
    """Download the package tarball (size capped) and look for V1/V2 markers in its source files."""
    dist = manifest.get("dist") or {}
    url = dist.get("tarball")
    if not url or int(dist.get("unpackedSize") or 0) > 8_000_000:
        return None
    data = (fetch or (lambda u: http_get(u, accept="*/*")))(url)
    if not data or len(data) > max_bytes:
        return None
    v1 = v2 = False
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            for m in tf:
                if not m.isfile() or m.size > 2_000_000 or not m.name.endswith(SOURCE_EXT):
                    continue
                f = tf.extractfile(m)
                a, b = scan_text(f.read().decode("utf-8", "replace")) if f else (False, False)
                v1, v2 = v1 or a, v2 or b
    except (tarfile.TarError, OSError, EOFError):
        return None
    return v1, v2


# ---------------------------------------------------------------- board data

def collect(candidates: Dict[str, dict], fetch: Callable[[str], Optional[dict]], old: Dict[str, dict], workers: int = 12,
            scan: Optional[Callable[[dict], Optional[Tuple[bool, bool]]]] = None, scan_limit: int = 800,
            log: Callable[[str], None] = lambda m: None) -> Tuple[Dict[str, dict], List[str]]:
    """Fetch the latest manifest of every candidate and classify it. Returns (records, errors)."""
    errors: List[str] = []
    records: Dict[str, dict] = {}
    scans = [0]

    def one(item):
        name, meta = item
        try:
            mf = fetch(name)
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (name, e))
            return
        if not mf:
            return
        cls = classify(mf)
        prev = old.get(name, {})
        code = None
        if cls["status"] == "unclear":
            if prev.get("scanned") == mf.get("version") and "code_v1" in prev:
                code = (prev["code_v1"], prev["code_v2"])
            elif scan and scans[0] < scan_limit:
                scans[0] += 1
                try:
                    code = scan(mf)
                except Exception as e:  # noqa: BLE001
                    errors.append("%s: scan %s" % (name, e))
            if code is not None:
                cls = classify(mf, code)
        rec = {"version": mf.get("version"), "weekly": meta.get("weekly", 0), "desc": (mf.get("description") or "")[:200],
               "repo": repo_slug(mf), "status": cls["status"], "evidence": cls["evidence"],
               "v1_range": cls["v1_range"], "v2_range": cls["v2_range"]}
        if code is not None:
            rec.update(scanned=mf.get("version"), code_v1=code[0], code_v2=code[1])
        records[name] = rec

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, list(candidates.items())))
    log("fetched %d of %d, %d scans, %d errors" % (len(records), len(candidates), scans[0], len(errors)))
    return records, errors


def candidates_from_search(objects: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for o in objects:
        p = o.get("package") or {}
        if p.get("name"):
            out[p["name"]] = {"weekly": int((o.get("downloads") or {}).get("weekly") or 0)}
    return out


def find_v2_issues(token: str, repos: set, fetch: Optional[Callable[[str], Optional[bytes]]] = None) -> Dict[str, str]:
    """Map repo slug -> URL of an open GitHub issue asking for OpenCode V2 support (needs a token for the search quota)."""
    get = fetch or (lambda u: http_get(u, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}))
    found: Dict[str, str] = {}
    for q in ("opencode v2 plugin", "opencode 2.0 plugin", "opencode v2 support"):
        for page in range(1, 11):
            url = "https://api.github.com/search/issues?q=%s&per_page=100&page=%d" % (
                urllib.parse.quote(q + " in:title is:issue is:open"), page)
            try:
                data = get(url)
            except urllib.error.HTTPError as e:
                if e.code == 422:  # past the 1000-result window of the search API
                    break
                raise
            items = json.loads(data).get("items", []) if data else []
            for it in items:
                m = re.search(r"repos/([^/]+/[^/]+)$", it.get("repository_url", ""))
                if m and m.group(1) in repos and re.search(r"v2|2\.0|opencode 2|@opencode/plugin", it.get("title", ""), re.I):
                    found.setdefault(m.group(1), it.get("html_url"))
            if len(items) < 100:
                break
            time.sleep(2.5)
    return found


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"packages": {}, "flips": [], "updated": None}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"), sort_keys=True)


def update_state(state: dict, fresh: Dict[str, dict], issues: Dict[str, str], today: str, versions: Dict[str, str]) -> dict:
    old = state.get("packages", {})
    flips = list(state.get("flips", []))
    packages: Dict[str, dict] = {}
    for name, rec in fresh.items():
        prev = old.get(name)
        rec = dict(rec)
        if prev and prev["status"] == rec["status"]:
            rec["since"] = prev.get("since")
        else:
            rec["since"] = today if prev else None
            if prev:
                flips.append({"date": today, "package": name, "from": prev["status"], "to": rec["status"], "version": rec["version"]})
        if rec["status"] in ("v1", "unclear") and rec.get("repo") and rec["repo"] in issues:
            rec["issue"] = issues[rec["repo"]]
        elif prev and prev.get("issue") and rec["status"] in ("v1", "unclear") and not issues:
            rec["issue"] = prev["issue"]  # no token this run: keep what we knew
        packages[name] = rec
    for name, prev in old.items():  # a transient fetch failure must not drop a package
        packages.setdefault(name, prev)
    ranked = sorted(packages, key=lambda n: -packages[n].get("weekly", 0))
    for i, n in enumerate(ranked, 1):
        packages[n]["rank"] = i
    return {"packages": packages, "flips": flips[-500:], "updated": today, "versions": versions}


# ---------------------------------------------------------------- site

def h(s) -> str:
    return html.escape(str(s), quote=True)


def slug(name: str) -> str:
    return name.replace("@", "").replace("/", "__")


CSS = """body{font:16px/1.5 system-ui,sans-serif;max-width:60rem;margin:0 auto;padding:1rem;color:#222}
table{border-collapse:collapse;width:100%}th,td{padding:.25rem .5rem;border-bottom:1px solid #ddd;text-align:left}
th{cursor:pointer}.s-ready,.s-dual{color:#0a7d2c}.s-v1{color:#b3261e}.s-unclear{color:#666}
code,textarea,pre{background:#f3f3f3;padding:0 .25rem}pre{padding:.5rem;overflow:auto}footer{margin-top:2rem;font-size:.9rem;color:#555}
input{padding:.3rem;width:100%;max-width:20rem}nav a{margin-right:1rem}textarea{width:100%;height:10rem;font-family:monospace}"""

# Client side: sort/filter tables and check a pasted opencode.json against data.json, then the npm registry (CORS *).
JS = r"""var q=document.getElementById('q');if(q){q.oninput=function(){var v=q.value.toLowerCase();
document.querySelectorAll('tbody tr').forEach(function(r){r.style.display=r.textContent.toLowerCase().indexOf(v)<0?'none':''})}}
document.querySelectorAll('th').forEach(function(th,i){th.onclick=function(){var b=th.closest('table').tBodies[0],
rs=[].slice.call(b.rows),d=th.dataset.d=th.dataset.d==='1'?'-1':'1';rs.sort(function(a,c){var x=a.cells[i].dataset.v||a.cells[i].textContent,
y=c.cells[i].dataset.v||c.cells[i].textContent,nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny))return(nx-ny)*d;return x.localeCompare(y)*d});
rs.forEach(function(r){b.appendChild(r)})}})
function strip(s){var o='',i=0,c;while(i<s.length){c=s[i];if(c=='"'){var j=i+1;while(j<s.length&&s[j]!='"'){if(s[j]=='\\')j++;j++}o+=s.slice(i,j+1);i=j+1}
else if(c=='/'&&s[i+1]=='/'){while(i<s.length&&s[i]!='\n')i++}else if(c=='/'&&s[i+1]=='*'){i=s.indexOf('*/',i+2);i=i<0?s.length:i+2}else{o+=c;i++}}
return o.replace(/,(\s*[}\]])/g,'$1')}
function nm(e){var s=typeof e=='string'?e:Array.isArray(e)?e[0]:e&&e.package;if(typeof s!='string')return null;if(/^(\.|\/|~|file:|github:|git\+|-|\*)/.test(s))return null;
var m=s.match(/^(@[^/@]+\/[^@]+|[^@]+)(?:@.*)?$/);return m?m[1]:null}
function cls(m){var d=function(k){return(m.dependencies||{})[k]||(m.peerDependencies||{})[k]||(m.optionalDependencies||{})[k]||(m.devDependencies||{})[k]},
a=!!d('@opencode-ai/plugin'),b=!!d('@opencode/plugin');return a&&b?'dual':b?'ready':a?'v1':'unclear'}
var T={ready:'V2 ready',dual:'V1 + V2',v1:'V1 only - will not load in OpenCode 2',unclear:'unclear (no plugin dependency declared)'};
var oc=document.getElementById('oc');if(oc){document.getElementById('go').onclick=function(){var out=document.getElementById('res'),cfg;
try{cfg=JSON.parse(strip(oc.value))}catch(e){out.textContent='Not valid JSON(C): '+e.message;return}
var list=[].concat(cfg.plugins||[],cfg.plugin||[]),names=list.map(nm).filter(Boolean);
if(!names.length){out.textContent='No npm plugins found in "plugin"/"plugins".';return}out.textContent='Checking '+names.length+' plugin(s)...';
fetch('data.json').then(function(r){return r.json()}).then(function(d){return Promise.all(names.map(function(n){var p=d.packages[n];if(p)return Promise.resolve(n+': '+T[p.status]+' ('+p.version+')');
return fetch('https://registry.npmjs.org/'+n.replace('/','%2F')+'/latest').then(function(r){return r.json()}).then(function(m){return n+': '+T[cls(m)]+' ('+m.version+')'}).catch(function(){return n+': not found on npm'})}))})
.then(function(rows){out.textContent=rows.join('\n')})}}"""


def page(title: str, body: str, base: str, desc: str = "") -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><meta name=description content='%s'><style>%s</style></head><body>"
            "<nav><a href='%s/'>opencode-v2-ready</a><a href='%s/plugins/'>All plugins</a><a href='%s/port/'>Port a plugin</a>"
            "<a href='%s/data.json'>data.json</a><a href='%s/flips.xml'>RSS</a><a href='%s'>GitHub</a></nav>%s"
            "<footer>Declared dependencies of the latest npm release of every package tagged <code>opencode-plugin</code>; bundled plugins are "
            "judged by scanning their code (marked <i>code</i>). A plugin on the V2 API is not proof it works in every setup. "
            "Unofficial, not affiliated with OpenCode or Anomaly. Useful? <a href='%s'>Donate via PayPal</a> &middot; "
            "<a href='%s'>source (MIT)</a></footer><script>%s</script></body></html>"
            % (h(title), h(desc), CSS, h(base), h(base), h(base), h(base), h(base), REPO, body, DONATE, REPO, JS))


def counts(state: dict) -> Dict[str, int]:
    c = {s: 0 for s in STATUS}
    for r in state["packages"].values():
        c[r["status"]] += 1
    return c


def fmt_status(r: dict) -> str:
    return "<span class='s-%s'>%s</span>%s" % (r["status"], h(STATUS[r["status"]][0]), " <i>code</i>" if r.get("evidence") == "code" else "")


def pkg_link(base: str, name: str) -> str:
    return "<a href='%s/p/%s.html'>%s</a>" % (h(base), h(slug(name)), h(name))


def render_index(state: dict, base: str) -> str:
    c = counts(state)
    pk = state["packages"]
    blockers = sorted((n for n, r in pk.items() if r["status"] == "v1"), key=lambda n: -pk[n]["weekly"])[:25]
    brows = "".join("<tr><td>%s</td><td data-v='%d'>%s</td><td>%s</td><td>%s</td></tr>"
                    % (pkg_link(base, n), pk[n]["weekly"], format(pk[n]["weekly"], ","), h(pk[n]["version"]),
                       "<a href='%s'>V2 issue</a>" % h(pk[n]["issue"]) if pk[n].get("issue") else "") for n in blockers)
    recent = "".join("<li>%s: %s %s &rarr; %s</li>" % (h(f["date"]), pkg_link(base, f["package"]), h(STATUS[f["from"]][0]), h(STATUS[f["to"]][0]))
                     for f in reversed(state["flips"][-15:])) or "<li>none recorded yet (the board started %s)</li>" % h(state["updated"])
    v = state.get("versions", {})
    body = ("<h1>Is your OpenCode plugin ready for V2?</h1>"
            "<p>OpenCode 2.0 has a new plugin API and <b>V1 plugin implementations do not run in V2</b> (<a href='%s'>official migration guide</a>). "
            "Of %d npm packages tagged <code>opencode-plugin</code>: <b class='s-ready'>%d</b> are on the V2 API, <b class='s-dual'>%d</b> support both, "
            "<b class='s-v1'>%d</b> are V1 only, %d are unclear. Updated %s (<code>%s</code> %s, <code>%s</code> %s).</p>"
            "<h2>Check your opencode.json</h2><p>Paste it (nothing is uploaded; only plugin names go to the npm registry), or run "
            "<code>pip install opencode-v2-ready &amp;&amp; opencode-v2-ready check</code> in your project.</p>"
            "<textarea id=oc placeholder='{ \"plugin\": [\"opencode-wakatime\", \"@acme/opencode-plugin\"] }'></textarea>"
            "<p><button id=go>Check plugins</button></p><pre id=res></pre>"
            "<h2>Seeing this error?</h2><p><code>%s</code> or <code>%s</code>: the plugin still exports a V1 function. "
            "Update it, or ask the author; see <a href='%s/port/'>how to port a plugin</a>.</p>"
            "<h2>Most used plugins that are V1 only</h2><table><thead><tr><th>Plugin</th><th>Weekly downloads</th><th>Version</th><th>Tracking</th></tr></thead>"
            "<tbody>%s</tbody></table><p><a href='%s/plugins/'>All %d plugins</a></p>"
            "<h2>Recent changes</h2><ul>%s</ul>"
            "<h2>Add a badge to your README</h2><p>Authors: <code>[![OpenCode V2](https://img.shields.io/endpoint?url=%s/badges/YOUR-PACKAGE.json)](%s/)</code> "
            "(scoped names: <code>@scope/name</code> becomes <code>scope__name</code>).</p>"
            % (h(MIGRATE_DOC), len(pk), c["ready"], c["dual"], c["v1"], c["unclear"], h(state["updated"]), h(V1_PKG), h(v.get("v1", "?")),
               h(V2_PKG), h(v.get("v2", "?")), h(ERRORS[0]), h(ERRORS[1]), h(base), brows, h(base), len(pk), recent, h(base), h(base)))
    return page("Is your OpenCode plugin ready for V2? Plugin compatibility board", body, base,
                "Which OpenCode plugins run on OpenCode 2.0: a board of npm plugins on the V1 or V2 plugin API, plus an opencode.json checker.")


def render_list(state: dict, base: str) -> str:
    rows = []
    for n, r in sorted(state["packages"].items(), key=lambda kv: kv[1]["rank"]):
        rows.append("<tr><td data-v='%d'>%s</td><td class='s-%s' data-v='%d'>%s</td><td data-v='%d'>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (r["rank"], pkg_link(base, n), r["status"], STATUS_ORDER.index(r["status"]), fmt_status(r), r["weekly"],
                       format(r["weekly"], ","), h(r["version"]), h(r.get("since") or "before tracking"),
                       "<a href='%s'>V2 issue</a>" % h(r["issue"]) if r.get("issue") else ""))
    body = ("<h1>All OpenCode plugins on npm</h1><p>Sorted by weekly downloads. Click a header to sort.</p>"
            "<p><input id=q placeholder='filter, e.g. v1 or wakatime'></p>"
            "<table><thead><tr><th>Plugin</th><th>Status</th><th>Weekly</th><th>Version</th><th>Since</th><th>Tracking</th></tr></thead><tbody>%s</tbody></table>"
            % "".join(rows))
    return page("All OpenCode plugins and their V2 status", body, base, "Every npm package tagged opencode-plugin with its V1/V2 plugin API status.")


def render_package(name: str, r: dict, base: str) -> str:
    text, _color, ok = STATUS[r["status"]]
    hint = {"v1": "It depends on the V1 plugin API and will not load in OpenCode 2 until the author ports it.",
            "unclear": "It declares no dependency on either plugin package, and no marker was found in its code.",
            "dual": "It ships both implementations.", "ready": "It targets the V2 plugin API."}[r["status"]]
    ev = "from its declared dependencies" if r.get("evidence") == "dependency" else "by scanning its code (heuristic)" if r.get("evidence") == "code" else "no evidence"
    badge = "https://img.shields.io/endpoint?url=%s/badges/%s.json" % (base, slug(name))
    issue = "<p>V2 request: <a href='%s'>%s</a></p>" % (h(r["issue"]), h(r["issue"])) if r.get("issue") else ""
    repo = " &middot; <a href='https://github.com/%s'>repository</a>" % h(r["repo"]) if r.get("repo") else ""
    body = ("<h1>%s: %s</h1><p>%s</p><p>Version %s, %s weekly downloads, rank %d. Judged %s.</p>"
            "<table><tbody><tr><th>%s</th><td><code>%s</code></td></tr><tr><th>%s</th><td><code>%s</code></td></tr></tbody></table>%s"
            "<p><a href='https://www.npmjs.com/package/%s'>npm</a>%s</p>"
            "<h2>Badge</h2><p><code>[![OpenCode V2](%s)](%s/p/%s.html)</code></p>"
            "<p>%s</p>"
            % (h(name), h(text), h(r["desc"]), h(r["version"]), format(r["weekly"], ","), r["rank"], h(ev), h(V1_PKG), h(r["v1_range"] or "-"),
               h(V2_PKG), h(r["v2_range"] or "-"), issue, h(name), repo, h(badge), h(base), h(slug(name)),
               h(hint) + (" See <a href='%s/port/'>how to port</a>." % h(base) if not ok else "")))
    return page("Does %s work with OpenCode 2?" % name, body, base, "%s %s: %s" % (name, r["version"], text))


def render_port(base: str) -> str:
    rows = "".join("<tr><td><code>%s</code></td><td><code>%s</code></td></tr>" % (h(a), h(b)) for a, b in HOOK_MAP)
    body = ("<h1>Port an OpenCode plugin from V1 to V2</h1><p>The official guide: <a href='%s'>Migrate V1 plugins</a>. "
            "There is no automatic conversion. Summary of the mechanical steps:</p><ol>"
            "<li>Depend on <code>%s</code> instead of <code>%s</code>.</li>"
            "<li>Replace the exported function with <code>export default Plugin.define({ id: \"my.plugin\", async setup(ctx) { ... } })</code>; the <code>id</code> is required.</li>"
            "<li>Read <code>directory</code>/<code>project</code> from <code>ctx.location</code>, options from <code>ctx.options</code>, and use domain methods instead of <code>client</code>.</li>"
            "<li>Register hooks inside <code>setup</code> (table below); the callback gets one mutable event.</li>"
            "<li>In opencode.json rename <code>plugin</code> to <code>plugins</code>; <code>[\"pkg\", {opts}]</code> becomes <code>{ \"package\": \"pkg\", \"options\": {opts} }</code>.</li></ol>"
            "<h2>Hook map</h2><table><thead><tr><th>V1</th><th>V2 (closest equivalent)</th></tr></thead><tbody>%s</tbody></table>"
            "<p>Using an AI agent? Give it <a href='%s/SKILL.md'>SKILL.md</a> (port-opencode-plugin-v2), also shipped in the "
            "<code>opencode-v2-ready</code> package: <code>opencode-v2-ready skill</code>.</p>"
            "<p>To stay compatible with both for a while, export <code>{ ...Plugin.define({...}), async server() { return { /* V1 hooks */ } } }</code> (V1 object entrypoints work from OpenCode 1.18.29).</p>"
            % (h(MIGRATE_DOC), h(V2_PKG), h(V1_PKG), rows, h(base)))
    return page("Port an OpenCode plugin from V1 to V2: hook map", body, base, "V1 to V2 OpenCode plugin hook map and migration steps.")


def badge_for(r: dict) -> dict:
    text, color, _ok = STATUS[r["status"]]
    return {"schemaVersion": 1, "label": "OpenCode V2", "message": text, "color": color}


def render_rss(state: dict, base: str) -> str:
    items = "".join("<item><title>%s: %s -> %s</title><link>%s/p/%s.html</link><guid>%s-%s-%s</guid><description>version %s</description></item>"
                    % (h(f["package"]), h(f["from"]), h(f["to"]), h(base), h(slug(f["package"])), h(f["date"]), h(f["package"]),
                       h(f["to"]), h(f["version"])) for f in reversed(state["flips"][-100:]))
    return ("<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel><title>opencode-v2-ready changes</title><link>%s/</link>"
            "<description>OpenCode plugins whose V1/V2 status changed</description>%s</channel></rss>" % (h(base), items))


def skill_text() -> str:
    with open(os.path.join(os.path.dirname(__file__), "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def render_site(state: dict, out: str, base: str = DEFAULT_BASE) -> int:
    base = base.rstrip("/")
    for sub in ("p", "badges", "plugins", "port"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    def write(rel, text):
        with open(os.path.join(out, rel), "w", encoding="utf-8") as f:
            f.write(text)

    write("index.html", render_index(state, base))
    write("plugins/index.html", render_list(state, base))
    write("port/index.html", render_port(base))
    write("SKILL.md", skill_text())
    for name, r in state["packages"].items():
        write("p/%s.html" % slug(name), render_package(name, r, base))
        write("badges/%s.json" % slug(name), json.dumps(badge_for(r)))
    write("data.json", json.dumps({"updated": state["updated"], "versions": state.get("versions", {}), "packages": state["packages"]},
                                  separators=(",", ":")))
    write("flips.xml", render_rss(state, base))
    urls = ["%s/" % base, "%s/plugins/" % base, "%s/port/" % base] + ["%s/p/%s.html" % (base, slug(k)) for k in state["packages"]]
    write("sitemap.xml", "<?xml version='1.0' encoding='utf-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>%s</urlset>"
          % "".join("<url><loc>%s</loc></url>" % h(u) for u in urls))
    write("robots.txt", "User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base)
    open(os.path.join(out, ".nojekyll"), "w").close()
    return len(state["packages"])


# ---------------------------------------------------------------- opencode.json reading

def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, leaving string contents alone."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    res: List[str] = []
    i, n = 0, len(s)
    while i < n:  # trailing commas, again string-aware
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            res.append(s[i:j + 1])
            i = j + 1
            continue
        if c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                i += 1
                continue
        res.append(c)
        i += 1
    return "".join(res)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = json.loads(strip_jsonc(f.read()))
    if not isinstance(doc, dict):
        raise ValueError("top level must be an object")
    return doc


class Entry:
    """One plugin reference from a config file."""

    def __init__(self, spec: str, options=None, key: str = "plugins", form: str = "string", source: str = ""):
        self.spec, self.options, self.key, self.form, self.source = spec, options, key, form, source
        self.kind = entry_kind(spec)
        self.name, self.version = split_spec(spec) if self.kind == "npm" else (spec, None)


def entry_kind(spec: str) -> str:
    if spec in ("*",) or spec.startswith("-") or spec.endswith(".*"):
        return "control"
    if spec.startswith(("./", "../", "/", "~", "file:", ".\\", "..\\")) or re.match(r"^[A-Za-z]:[\\/]", spec):
        return "local"
    if spec.startswith(("github:", "git+", "git:", "http:", "https:", "ssh:", "gitlab:", "bitbucket:")) or "#" in spec:
        return "git"
    return "npm"


def split_spec(spec: str) -> Tuple[str, Optional[str]]:
    m = re.match(r"^(@[^/@]+/[^@]+|[^@]+)(?:@(.+))?$", spec)
    return (m.group(1), m.group(2)) if m else (spec, None)


def entries_from_config(doc: dict, source: str = "") -> Tuple[List[Entry], List[str]]:
    entries: List[Entry] = []
    warnings: List[str] = []
    for key in ("plugin", "plugins"):
        val = doc.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            warnings.append('"%s" should be an array' % key)
            continue
        for item in val:
            if isinstance(item, str):
                entries.append(Entry(item, None, key, "string", source))
            elif isinstance(item, list) and item and isinstance(item[0], str):
                entries.append(Entry(item[0], item[1] if len(item) > 1 else None, key, "tuple", source))
            elif isinstance(item, dict) and isinstance(item.get("package"), str):
                entries.append(Entry(item["package"], item.get("options"), key, "object", source))
            else:
                warnings.append('"%s" has an entry I do not understand: %s' % (key, json.dumps(item)[:80]))
    return entries, warnings


def v2_form(e: Entry):
    if e.options is None:
        return e.spec
    return {"package": e.spec, "options": e.options}


def scan_path(path: str, limit_files: int = 300) -> Optional[Tuple[bool, bool]]:
    """Scan a local plugin file or directory for V1/V2 markers (and its package.json dependencies)."""
    if not os.path.exists(path):
        return None
    v1 = v2 = False
    files: List[str] = []
    if os.path.isfile(path):
        files = [path]
    else:
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist-types")]
            files += [os.path.join(root, n) for n in names if n.endswith(SOURCE_EXT) or n == "package.json"]
            if len(files) >= limit_files:
                break
    for fp in files[:limit_files]:
        try:
            if os.path.getsize(fp) > 2_000_000:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if fp.endswith("package.json"):
            try:
                cls = classify(json.loads(text))
                v1, v2 = v1 or cls["v1_range"] is not None, v2 or cls["v2_range"] is not None
            except ValueError:
                pass
            continue
        a, b = scan_text(text)
        v1, v2 = v1 or a, v2 or b
    return v1, v2


# ---------------------------------------------------------------- check

def check_entry(e: Entry, base_dir: str, fetch: Callable[[str, str], Optional[dict]]) -> dict:
    """Return a result dict: status, detail, plus ``latest`` when a pinned version differs."""
    res = {"spec": e.spec, "kind": e.kind, "key": e.key, "source": e.source}
    if e.kind == "control":
        res.update(status="skipped", detail="enable/disable rule (V2 syntax)")
        return res
    if e.kind == "git":
        res.update(status="skipped", detail="git source: not checked; open its package.json and look for %s" % V2_PKG)
        return res
    if e.kind == "local":
        p = e.spec[len("file://"):] if e.spec.startswith("file://") else e.spec
        p = os.path.expanduser(p)
        full = p if os.path.isabs(p) else os.path.join(base_dir, p)
        code = scan_path(full)
        if code is None:
            res.update(status="missing", detail="path not found: %s" % full)
            return res
        cls = classify({}, code)
        res.update(status=cls["status"], detail="local files scanned (heuristic)" if cls["evidence"] == "code" else "no V1/V2 markers in the local files")
        return res
    exact = bool(e.version and re.match(r"^\d+\.\d+\.\d+(?:-[\w.]+)?$", e.version))
    try:
        latest = fetch(e.name, "latest")
        pinned = fetch(e.name, e.version) if exact else latest
    except Exception as ex:  # noqa: BLE001
        res.update(status="error", detail="registry lookup failed: %s" % ex)
        return res
    if latest is None or pinned is None:
        res.update(status="missing", detail="not found on npm (or that version does not exist)")
        return res
    cls, lcls = classify(pinned), classify(latest)
    if cls["status"] == "unclear":
        code = scan_tarball(pinned)
        if code:
            cls = classify(pinned, code)
    if lcls["status"] == "unclear" and latest is not pinned:
        code = scan_tarball(latest)
        lcls = classify(latest, code) if code else lcls
    res.update(status=cls["status"], version=pinned.get("version"), evidence=cls["evidence"])
    if exact and lcls["status"] != cls["status"]:
        res.update(latest=lcls["status"], latest_version=latest.get("version"))
    return res


def describe(res: dict) -> str:
    text = STATUS.get(res["status"], (res["status"],))[0]
    line = "%s%s: %s" % (res["spec"], "", text)
    if res.get("version"):
        line += " (%s%s)" % (res["version"], ", by code scan" if res.get("evidence") == "code" else "")
    if res.get("latest"):
        line += "; latest %s is %s" % (res["latest_version"], STATUS[res["latest"]][0])
    if res.get("detail"):
        line += " - " + res["detail"]
    if res["status"] == "v1":
        line += " - will not load in OpenCode 2"
    return line


def find_configs(target: str) -> Tuple[List[str], List[str]]:
    """Return (config files, local plugin dirs/files) for a path: a config file or a project/config directory."""
    if os.path.isfile(target):
        return [target], []
    cfgs: List[str] = []
    locs: List[str] = []
    for rel in ("opencode.json", "opencode.jsonc", os.path.join(".opencode", "opencode.json"), os.path.join(".opencode", "opencode.jsonc"),
                "tui.json", "tui.jsonc", "cli.json", "cli.jsonc"):
        p = os.path.join(target, rel)
        if os.path.isfile(p):
            cfgs.append(p)
    for pl in (".opencode/plugins", ".opencode/plugin", "plugins", "plugin"):
        d = os.path.join(target, *pl.split("/"))
        if os.path.isdir(d) and (pl.startswith(".opencode") or os.path.basename(target) == "opencode"):
            for n in sorted(os.listdir(d)):
                if not n.startswith("."):
                    locs.append(os.path.join(d, n))
    return cfgs, locs


def cmd_check(args) -> int:
    targets = list(args.paths)
    if args.global_config:
        targets.append(os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "opencode"))
    if not targets and not args.packages:
        targets = ["."]
    entries: List[Tuple[Entry, str]] = []
    warnings: List[str] = []
    for t in targets:
        if not os.path.exists(t):
            print("opencode-v2-ready: no such file or directory: %s" % t, file=sys.stderr)
            return 2
        cfgs, locs = find_configs(t)
        if not cfgs and not locs:
            print("opencode-v2-ready: no opencode.json(c) or .opencode/plugins found in %s" % t, file=sys.stderr)
            if len(targets) == 1 and not args.packages:
                return 2
        for c in cfgs:
            try:
                es, ws = entries_from_config(load_config(c), c)
            except (OSError, ValueError) as ex:
                print("opencode-v2-ready: cannot read %s: %s" % (c, ex), file=sys.stderr)
                return 2
            entries += [(e, os.path.dirname(os.path.abspath(c))) for e in es]
            warnings += ["%s: %s" % (c, w) for w in ws]
        for loc in locs:
            entries.append((Entry(os.path.abspath(loc), None, "plugins/ directory", "path", loc), "/"))
    for name in args.packages or []:
        entries.append((Entry(name, None, "-p", "string", "-p"), "."))
    if not entries:
        print("no plugins configured")
        return 0
    fetch = fetch_manifest
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(lambda eb: check_entry(eb[0], eb[1], fetch), entries))
    for (e, _b), r in zip(entries, results):
        if e.form == "path":
            r["spec"] = e.source
    counts_ = {}
    for r in results:
        counts_[r["status"]] = counts_.get(r["status"], 0) + 1
    legacy = [e for e, _ in entries if e.key == "plugin" or e.form == "tuple"]
    if args.json:
        print(json.dumps({"results": results, "warnings": warnings, "counts": counts_,
                          "v2_plugins": [v2_form(e) for e, _ in entries if e.kind != "control" and e.key in ("plugin", "plugins")]}, indent=2))
    else:
        for r in results:
            if r["status"] in ("skipped",) and not args.all:
                continue
            mark = {"ready": "ok  ", "dual": "ok  ", "v1": "FAIL", "unclear": "?   ", "missing": "??  ", "error": "ERR ", "skipped": "-   "}[r["status"]]
            print("%s %s" % (mark, describe(r)))
        for w in warnings:
            print("warn " + w)
        if legacy:
            print('note: V1 syntax ("plugin" key or ["pkg", {...}] tuples) is still read by OpenCode 2; the native V2 form is:')
            arr = [v2_form(e) for e, _ in entries if e.kind != "control" and e.key in ("plugin", "plugins")]
            print(json.dumps({"plugins": arr}, indent=2))
        print("%d plugin(s): %s" % (len(results), ", ".join("%d %s" % (v, k) for k, v in sorted(counts_.items()))))
    if counts_.get("error") or counts_.get("missing"):
        pass
    if args.fail_on_v1 and counts_.get("v1"):
        return 1
    if counts_.get("error"):
        return 3
    return 0


# ---------------------------------------------------------------- build / skill

def cmd_build(args) -> int:
    today = args.today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    state_path = os.path.join(args.data, "state.json")
    state = load_state(state_path)
    if not args.no_fetch:
        try:
            objs: List[dict] = []
            for q in SEARCH_QUERIES:
                objs += fetch_search(q)
            cand = candidates_from_search(objs)
            versions = {"v1": (fetch_manifest(V1_PKG) or {}).get("version", "?"), "v2": (fetch_manifest(V2_PKG) or {}).get("version", "?")}
        except Exception as e:  # noqa: BLE001
            print("opencode-v2-ready: %s" % e, file=sys.stderr)
            return 3
        if len(cand) < 500:
            print("opencode-v2-ready: search returned only %d packages, keeping old data" % len(cand), file=sys.stderr)
            return 3
        fresh, errors = collect(cand, fetch_manifest, state.get("packages", {}), args.workers, scan=scan_tarball,
                                log=lambda m: print(m, file=sys.stderr))
        if len(fresh) < len(cand) * 0.8:
            print("opencode-v2-ready: only %d of %d packages fetched, keeping old data" % (len(fresh), len(cand)), file=sys.stderr)
            return 3
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_ACCESS_TOKEN")
        issues: Dict[str, str] = {}
        if token and not args.no_issues:
            repos = {r["repo"] for r in fresh.values() if r["status"] in ("v1", "unclear") and r.get("repo")}
            try:
                issues = find_v2_issues(token, repos)
            except Exception as e:  # noqa: BLE001
                print("warning: issue search failed: %s" % e, file=sys.stderr)
        state = update_state(state, fresh, issues, today, versions)
        save_state(state_path, state)
        for e in errors[:20]:
            print("warning: " + e, file=sys.stderr)
    elif not state.get("packages"):
        print("opencode-v2-ready: no data in %s (run without --no-fetch first)" % state_path, file=sys.stderr)
        return 2
    n = render_site(state, args.out, args.base_url)
    print("rendered %d package pages into %s" % (n, args.out))
    return 0


def cmd_skill(_args) -> int:
    sys.stdout.write(skill_text())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="opencode-v2-ready", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check the plugins in your opencode.json(c) / .opencode/ against the V2 plugin API (live npm registry)")
    c.add_argument("paths", nargs="*", help="opencode.json(c) files or project/config directories (default: current directory)")
    c.add_argument("-g", "--global", dest="global_config", action="store_true", help="also check ~/.config/opencode")
    c.add_argument("-p", "--package", dest="packages", action="append", help="check an npm plugin by name or name@version (repeatable)")
    c.add_argument("--all", action="store_true", help="also list skipped entries (git sources, enable/disable rules)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--fail-on-v1", action="store_true", help="exit 1 if any plugin is V1 only")
    c.add_argument("--workers", type=int, default=8)
    c.set_defaults(fn=cmd_check)
    b = sub.add_parser("build", help="scan npm for opencode plugins, update data/ and render the site")
    b.add_argument("--data", default="data")
    b.add_argument("--out", default="site")
    b.add_argument("--base-url", default=DEFAULT_BASE)
    b.add_argument("--workers", type=int, default=12)
    b.add_argument("--today", help="override the date (YYYY-MM-DD)")
    b.add_argument("--no-fetch", action="store_true", help="only re-render from data/state.json")
    b.add_argument("--no-issues", action="store_true", help="skip the GitHub issue search")
    b.set_defaults(fn=cmd_build)
    s = sub.add_parser("skill", help="print the bundled port-opencode-plugin-v2 SKILL.md")
    s.set_defaults(fn=cmd_skill)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
