# opencode-v2-ready

**Is your OpenCode plugin ready for OpenCode 2.0?** OpenCode 2.0 (released 2026-10-14) has a new plugin API, and
[V1 plugin implementations do not run in V2](https://opencode.ai/v2/docs/migrate-v1). There is no automatic conversion.
Users who upgrade find plugins silently missing, or failing with `default export must be an object with id` / `{ id, setup }`.

This project answers two questions:

- **Which plugins work?** A daily board of every npm package tagged `opencode-plugin`: V2 ready, V1 + V2, V1 only, unclear.
  Board: <https://rafal-one.github.io/opencode-v2-ready/>
- **Will my config load?** A CLI (and a paste-box on the board) that reads your `opencode.json(c)` and `.opencode/plugins/`,
  looks every plugin up on npm and tells you which ones are still on the V1 plugin API.

Plus a ready-made agent skill, `port-opencode-plugin-v2`, with the V1 to V2 hook map, for porting your own plugin.

Stdlib-only Python 3.9+.

## Check your setup

```console
$ pip install opencode-v2-ready
$ opencode-v2-ready check            # current project: opencode.json(c), .opencode/opencode.json(c), .opencode/plugins/
$ opencode-v2-ready check -g         # plus ~/.config/opencode
$ opencode-v2-ready check ~/.config/opencode/opencode.jsonc --fail-on-v1
ok   opencode-wakatime: V2 ready (2.0.1)
FAIL some-plugin@1.4.0: V1 only (1.4.0); latest 2.0.0 is V2 ready - will not load in OpenCode 2
FAIL ./plugins/mine.ts: V1 only - local files scanned (heuristic) - will not load in OpenCode 2
note: V1 syntax ("plugin" key or ["pkg", {...}] tuples) is still read by OpenCode 2; the native V2 form is:
{ "plugins": [ "opencode-wakatime", {"package": "some-plugin@1.4.0", "options": {...}} ] }
3 plugin(s): 1 ready, 2 v1
```

| Option | Meaning |
|---|---|
| `PATH...` | `opencode.json(c)` files or project/config directories (default `.`) |
| `-g`, `--global` | also check `~/.config/opencode` |
| `-p NAME[@VER]` | check an npm plugin by name without a config (repeatable) |
| `--fail-on-v1` | exit 1 if any plugin is V1 only (for CI or a pre-upgrade script) |
| `--json` | machine-readable output |
| `--all` | also list skipped entries (git sources, `-name` / `*` enable/disable rules) |

Exit codes: `0` ok, `1` V1-only plugin found with `--fail-on-v1`, `2` bad input (missing path, invalid JSON(C), nothing to check),
`3` npm registry unreachable for at least one plugin.

How a plugin is judged: its latest release (or the exact version you pinned) declares a dependency, peer or dev dependency on
`@opencode/plugin` (V2) and/or `@opencode-ai/plugin` (V1). Plugins that bundle everything declare neither; for those the tarball
(or your local file) is scanned for V1 hook keys (`"tool.execute.before"`, `"chat.params"`, ...) and V2 markers (`Plugin.define(`,
`ctx.tool.hook(`, ...) and the result is marked *by code scan*. That is a heuristic. A "V2 ready" plugin is on the new API;
it can still have bugs on it.

## Port your plugin

```console
$ opencode-v2-ready skill > .claude/skills/port-opencode-plugin-v2/SKILL.md     # or any agent's skills folder
```

`skills/port-opencode-plugin-v2/SKILL.md` has the symptoms, the steps, the hook map (`tool.execute.before` ->
`ctx.tool.hook("execute.before", ...)`, `chat.params` -> `ctx.session.hook("context", ...)`, ...) and the "support V1 and V2 from one
package" pattern. The same table is on the [port page](https://rafal-one.github.io/opencode-v2-ready/port/).

## Badge for plugin authors

```markdown
[![OpenCode V2](https://img.shields.io/endpoint?url=https://rafal-one.github.io/opencode-v2-ready/badges/YOUR-PACKAGE.json)](https://rafal-one.github.io/opencode-v2-ready/)
```

Scoped names: `@scope/name` becomes `scope__name`. The badge follows the board (V2 ready / V1 + V2 / V1 only).

## Build the board yourself

```console
$ opencode-v2-ready build --data data --out site     # ~5 min: npm search + one request per plugin + tarball scan of bundled ones
```

Set `GITHUB_TOKEN` to also find open "OpenCode V2 support" issues in the plugins' repositories (GitHub search quota).
`data/state.json` keeps history (when a plugin changed status, RSS in `flips.xml`). `ci/update.yml.example` is a daily GitHub Action.

## Testing

`pytest` (26 tests: classification, tarball and code scan, JSONC parsing, all config entry forms, pinned versions, local plugins,
bad input and exit codes, rendering and escaping). Run on Python 3.9 and 3.14; wheel and sdist installed into clean venvs.

## Support

Unofficial; not affiliated with OpenCode / Anomaly. If this saved you an evening, you can
[donate via PayPal](https://paypal.me/RafalBaranowski). MIT licensed.
