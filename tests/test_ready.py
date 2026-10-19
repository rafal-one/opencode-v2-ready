import io
import json
import os
import tarfile

import pytest

import opencode_v2_ready as o

V1 = "@opencode-ai/plugin"
V2 = "@opencode/plugin"


def mf(deps=None, peer=None, dev=None, version="1.0.0", **kw):
    d = {"name": "x", "version": version, "dependencies": deps or {}, "peerDependencies": peer or {}, "devDependencies": dev or {}}
    d.update(kw)
    return d


# ---- classification
def test_classify_dependency_variants():
    assert o.classify(mf({V1: "^1"}))["status"] == "v1"
    assert o.classify(mf({V2: "^2"}))["status"] == "ready"
    assert o.classify(mf({V1: "^1", V2: "^2"}))["status"] == "dual"
    assert o.classify(mf(peer={V2: "*"}))["status"] == "ready"
    assert o.classify(mf(dev={V1: "1"}))["status"] == "v1"
    assert o.classify(mf())["status"] == "unclear"
    assert o.classify(mf({V1: "^1"}))["v1_range"] == "^1"


def test_classify_code_scan_only_when_no_dependency():
    assert o.classify(mf(), (True, False)) ["status"] == "v1"
    assert o.classify(mf(), (False, True))["evidence"] == "code"
    assert o.classify(mf(), (False, False))["evidence"] == "none"
    assert o.classify(mf({V2: "2"}), (True, False))["status"] == "ready"  # dependency wins


def test_scan_text_markers():
    assert o.scan_text('export default Plugin.define({ id: "a", setup(ctx) {} })') == (False, True)
    assert o.scan_text('return {"tool.execute.before": async () => {}}') == (True, False)
    assert o.scan_text("await ctx.tool.hook('execute.before', f)") == (False, True)
    assert o.scan_text("const x = 1") == (False, False)
    assert o.scan_text('import {tool} from "@opencode-ai/plugin"') == (True, False)


def test_repo_slug():
    assert o.repo_slug({"repository": {"url": "git+https://github.com/a/b.git"}}) == "a/b"
    assert o.repo_slug({"repository": "https://github.com/a/b/tree/main/pkg"}) == "a/b"
    assert o.repo_slug({"repository": "github:a/b"}) == "a/b"
    assert o.repo_slug({}) is None
    assert o.repo_slug({"repository": {"url": "https://gitlab.com/a/b"}}) is None


# ---- tarball scan
def make_tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_scan_tarball():
    m = {"dist": {"tarball": "http://x/t.tgz", "unpackedSize": 100}}
    tgz = make_tgz({"package/dist/index.js": "export default Plugin.define({id:'a'})", "package/README.md": "@opencode-ai/plugin"})
    assert o.scan_tarball(m, fetch=lambda u: tgz) == (False, True)
    assert o.scan_tarball(m, fetch=lambda u: b"not a tarball") is None
    assert o.scan_tarball(m, fetch=lambda u: None) is None
    assert o.scan_tarball({"dist": {}}, fetch=lambda u: tgz) is None
    assert o.scan_tarball({"dist": {"tarball": "u", "unpackedSize": 99_000_000}}, fetch=lambda u: tgz) is None
    assert o.scan_tarball(m, max_bytes=5, fetch=lambda u: tgz) is None


# ---- board data
def search_obj(name, weekly):
    return {"package": {"name": name}, "downloads": {"weekly": weekly}}


def test_collect_and_state_flip():
    cand = o.candidates_from_search([search_obj("a", 100), search_obj("b", 50), search_obj("c", 5), {"package": {}}])
    assert set(cand) == {"a", "b", "c"}
    manifests = {"a": mf({V1: "1"}, repository="https://github.com/o/a"), "b": mf({V2: "2"}), "c": mf()}
    calls = []
    fresh, errs = o.collect(cand, lambda n: manifests.get(n), {}, workers=2, scan=lambda m: calls.append(1) or (False, True))
    assert not errs and fresh["a"]["status"] == "v1" and fresh["b"]["status"] == "ready"
    assert fresh["c"]["status"] == "ready" and fresh["c"]["evidence"] == "code" and fresh["c"]["scanned"] == "1.0.0"
    st = o.update_state({"packages": {}, "flips": []}, fresh, {"o/a": "http://issue"}, "2026-10-19", {"v1": "1", "v2": "2"})
    assert st["packages"]["a"]["issue"] == "http://issue" and st["packages"]["a"]["since"] is None
    assert [st["packages"][n]["rank"] for n in "abc"] == [1, 2, 3] and not st["flips"]
    # a scanned result is reused for the same version (no second download)
    calls.clear()
    o.collect({"c": {"weekly": 5}}, lambda n: manifests[n], st["packages"], workers=1, scan=lambda m: calls.append(1))
    assert not calls
    # a plugin gets ported: flip recorded, issue dropped
    manifests["a"] = mf({V2: "2"}, version="2.0.0", repository="https://github.com/o/a")
    fresh2, _ = o.collect(cand, lambda n: manifests.get(n), st["packages"], workers=2)
    st2 = o.update_state(st, fresh2, {}, "2026-10-20", {})
    assert st2["flips"] == [{"date": "2026-10-20", "package": "a", "from": "v1", "to": "ready", "version": "2.0.0"}]
    assert st2["packages"]["a"]["since"] == "2026-10-20" and "issue" not in st2["packages"]["a"]


def test_collect_errors_and_missing_keep_old():
    def boom(n):
        if n == "bad":
            raise RuntimeError("boom")
        return None if n == "gone" else mf({V1: "1"})
    fresh, errs = o.collect({"bad": {}, "gone": {}, "ok": {}}, boom, {}, workers=1)
    assert list(fresh) == ["ok"] and errs == ["bad: boom"]
    old = {"bad": {"status": "v1", "weekly": 3, "version": "1", "since": None}}
    st = o.update_state({"packages": old, "flips": []}, fresh, {}, "d", {})
    assert "bad" in st["packages"] and st["packages"]["bad"]["rank"] == 1


def test_find_v2_issues():
    page = {"items": [
        {"repository_url": "https://api.github.com/repos/o/a", "title": "Request: OpenCode V2 plugin API support", "html_url": "http://i/1"},
        {"repository_url": "https://api.github.com/repos/o/a", "title": "OpenCode 2.0 broke it", "html_url": "http://i/2"},
        {"repository_url": "https://api.github.com/repos/o/zzz", "title": "V2 please", "html_url": "http://i/3"},
        {"repository_url": "https://api.github.com/repos/o/b", "title": "unrelated bug", "html_url": "http://i/4"}]}
    got = o.find_v2_issues("tok", {"o/a", "o/b"}, fetch=lambda u: json.dumps(page).encode())
    assert got == {"o/a": "http://i/1"}


# ---- rendering
def sample_state():
    cand = o.candidates_from_search([search_obj("@s/p", 100), search_obj("v1pkg", 50)])
    fresh, _ = o.collect(cand, lambda n: mf({V2: "2"}) if n == "@s/p" else mf({V1: "1"}), {})
    return o.update_state({"packages": {}, "flips": []}, fresh, {}, "2026-10-19", {"v1": "1.19.3", "v2": "2.0.32"})


def test_render_site(tmp_path):
    st = sample_state()
    assert o.render_site(st, str(tmp_path), "https://x.test/base/") == 2
    index = (tmp_path / "index.html").read_text()
    assert "V1 plugin implementations do not run in V2" in index and "v1pkg" in index and o.ERRORS[0] in index
    assert (tmp_path / "p" / "s__p.html").exists()
    assert json.loads((tmp_path / "badges" / "s__p.json").read_text())["message"] == "V2 ready"
    assert json.loads((tmp_path / "badges" / "v1pkg.json").read_text())["color"] == "red"
    assert json.loads((tmp_path / "data.json").read_text())["packages"]["v1pkg"]["status"] == "v1"
    port = (tmp_path / "port" / "index.html").read_text()
    assert all(k in port for k, _ in o.HOOK_MAP)
    assert (tmp_path / "SKILL.md").read_text().startswith("---\nname: port-opencode-plugin-v2")
    assert "paypal.me/RafalBaranowski" in index and "x.test/base/p/" in (tmp_path / "sitemap.xml").read_text()


def test_html_escaped(tmp_path):
    st = sample_state()
    st["packages"]["v1pkg"]["desc"] = "<script>alert(1)</script>"
    o.render_site(st, str(tmp_path))
    assert "<script>alert(1)" not in (tmp_path / "p" / "v1pkg.html").read_text()


def test_skill_files_in_sync_and_cover_hooks():
    here = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(here, "skills", "port-opencode-plugin-v2", "SKILL.md")) as f:
        assert f.read() == o.skill_text()
    for hook, _ in o.HOOK_MAP:
        assert "`%s`" % hook in o.skill_text()


# ---- config reading
def test_strip_jsonc():
    src = '{\n // c\n "a": "x // not a comment", /* b */ "b": [1, 2,],\n "c": "q\\"/*",\n}'
    assert json.loads(o.strip_jsonc(src)) == {"a": "x // not a comment", "b": [1, 2], "c": 'q"/*'}


def test_entries_from_config_forms():
    doc = {"plugin": ["a", ["b@1.2.3", {"k": 1}]], "plugins": [{"package": "@s/c", "options": {"x": 1}}, "./local", 5, "-a", "github:o/r", "*"]}
    es, ws = o.entries_from_config(doc)
    assert [e.kind for e in es] == ["npm", "npm", "npm", "local", "control", "git", "control"]
    assert (es[1].name, es[1].version, es[1].form) == ("b", "1.2.3", "tuple")
    assert es[2].name == "@s/c" and es[2].options == {"x": 1}
    assert len(ws) == 1 and "do not understand" in ws[0]
    assert o.v2_form(es[1]) == {"package": "b@1.2.3", "options": {"k": 1}} and o.v2_form(es[0]) == "a"
    assert o.entries_from_config({"plugin": "x"})[1] == ['"plugin" should be an array']


def test_split_spec():
    assert o.split_spec("@a/b@1.0.0") == ("@a/b", "1.0.0")
    assert o.split_spec("@a/b") == ("@a/b", None)
    assert o.split_spec("p@latest") == ("p", "latest")


# ---- check
def fake_fetch(table):
    def f(name, version="latest"):
        v = table.get((name, version)) or (table.get((name, "latest")) if version == "latest" else None)
        return v
    return f


def test_check_entry_npm_and_pinned():
    table = {("old", "latest"): mf({V2: "2"}, version="2.0.0"), ("old", "1.0.0"): mf({V1: "1"}, version="1.0.0"),
             ("plain", "latest"): mf({V1: "1"})}
    f = fake_fetch(table)
    r = o.check_entry(o.Entry("old@1.0.0"), ".", f)
    assert r["status"] == "v1" and r["latest"] == "ready" and r["latest_version"] == "2.0.0"
    assert "latest 2.0.0 is V2 ready" in o.describe(r) and "will not load" in o.describe(r)
    assert o.check_entry(o.Entry("plain"), ".", f)["status"] == "v1"
    assert o.check_entry(o.Entry("nope"), ".", f)["status"] == "missing"
    assert o.check_entry(o.Entry("old@9.9.9"), ".", f)["status"] == "missing"
    assert o.check_entry(o.Entry("github:o/r"), ".", f)["status"] == "skipped"
    assert o.check_entry(o.Entry("-old"), ".", f)["status"] == "skipped"


def test_check_entry_registry_error():
    def boom(n, v="latest"):
        raise OSError("down")
    r = o.check_entry(o.Entry("x"), ".", boom)
    assert r["status"] == "error" and "down" in r["detail"]


def test_check_entry_unclear_uses_tarball(monkeypatch):
    monkeypatch.setattr(o, "scan_tarball", lambda m: (False, True))
    r = o.check_entry(o.Entry("bundled"), ".", fake_fetch({("bundled", "latest"): mf()}))
    assert r["status"] == "ready" and r["evidence"] == "code"


def test_check_entry_local(tmp_path):
    (tmp_path / "v1.ts").write_text('export const P = async () => ({"tool.execute.before": async () => {}})')
    d = tmp_path / "v2dir"
    d.mkdir()
    (d / "index.ts").write_text('import { Plugin } from "@opencode/plugin"\nexport default Plugin.define({id:"a", setup(){}})')
    assert o.check_entry(o.Entry("./v1.ts"), str(tmp_path), None)["status"] == "v1"
    assert o.check_entry(o.Entry("./v2dir"), str(tmp_path), None)["status"] == "ready"
    assert o.check_entry(o.Entry("./nothing.ts"), str(tmp_path), None)["status"] == "missing"
    assert o.check_entry(o.Entry("file://" + str(d)), str(tmp_path), None)["status"] == "ready"


# ---- CLI
@pytest.fixture
def fake_registry(monkeypatch):
    table = {("good", "latest"): mf({V2: "2"}, version="2.1.0"), ("bad", "latest"): mf({V1: "1"}, version="1.4.0")}
    monkeypatch.setattr(o, "fetch_manifest", fake_fetch(table))
    monkeypatch.setattr(o, "scan_tarball", lambda m: None)


def write_project(tmp_path, cfg='{"plugin": ["good", ["bad", {"a": 1}]]}'):
    (tmp_path / "opencode.json").write_text(cfg)
    return str(tmp_path)


def test_cli_check_output_and_exit_codes(tmp_path, capsys, fake_registry):
    proj = write_project(tmp_path)
    assert o.main(["check", proj]) == 0
    out = capsys.readouterr().out
    assert "ok   good: V2 ready (2.1.0)" in out and "FAIL bad: V1 only" in out and '"plugins"' in out and "2 plugin(s)" in out
    assert o.main(["check", proj, "--fail-on-v1"]) == 1
    capsys.readouterr()
    assert o.main(["check", proj + "/opencode.json", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"ready": 1, "v1": 1} and data["v2_plugins"][1] == {"package": "bad", "options": {"a": 1}}


def test_cli_check_bad_input(tmp_path, capsys, fake_registry):
    assert o.main(["check", str(tmp_path / "missing")]) == 2
    assert o.main(["check", str(tmp_path)]) == 2  # empty dir
    p = tmp_path / "broken"
    p.mkdir()
    (p / "opencode.json").write_text("{not json")
    assert o.main(["check", str(p)]) == 2
    assert "cannot read" in capsys.readouterr().err
    (p / "opencode.json").write_text("[1]")
    assert o.main(["check", str(p)]) == 2


def test_cli_check_registry_down_exit_3(tmp_path, monkeypatch, capsys):
    def boom(n, v="latest"):
        raise OSError("offline")
    monkeypatch.setattr(o, "fetch_manifest", boom)
    assert o.main(["check", write_project(tmp_path, '{"plugins": ["x"]}')]) == 3
    assert "ERR" in capsys.readouterr().out


def test_cli_check_no_plugins_and_package_flag(tmp_path, capsys, fake_registry):
    assert o.main(["check", write_project(tmp_path, '{"model": "x"}')]) == 0
    assert "no plugins configured" in capsys.readouterr().out
    assert o.main(["check", "-p", "bad", "--fail-on-v1"]) == 1


def test_cli_finds_opencode_dir_plugins(tmp_path, capsys, fake_registry):
    pl = tmp_path / ".opencode" / "plugins"
    pl.mkdir(parents=True)
    (pl / "mine.ts").write_text('export default { "chat.params": 1 }; // "chat.params"')
    (tmp_path / ".opencode" / "opencode.jsonc").write_text('{ // c\n "plugins": ["good",], }')
    assert o.main(["check", str(tmp_path), "--fail-on-v1"]) == 1
    out = capsys.readouterr().out
    assert "mine.ts: V1 only" in out and "good: V2 ready" in out


def test_cli_skill_and_version(capsys):
    assert o.main(["skill"]) == 0
    assert "port-opencode-plugin-v2" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        o.main(["--version"])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        o.main([])
    assert e.value.code == 2


def test_build_no_fetch_needs_data(tmp_path, capsys):
    assert o.main(["build", "--no-fetch", "--data", str(tmp_path / "d"), "--out", str(tmp_path / "s")]) == 2


def test_build_no_fetch_renders(tmp_path):
    d = tmp_path / "d"
    o.save_state(str(d / "state.json"), sample_state())
    assert o.main(["build", "--no-fetch", "--data", str(d), "--out", str(tmp_path / "s")]) == 0
    assert (tmp_path / "s" / "index.html").exists()


def test_find_v2_issues_stops_on_422():
    import urllib.error

    def f(url):
        raise urllib.error.HTTPError(url, 422, "x", {}, None)
    assert o.find_v2_issues("t", {"o/a"}, fetch=f) == {}
