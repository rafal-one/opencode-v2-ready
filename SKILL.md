---
name: port-opencode-plugin-v2
description: Port an OpenCode 1.x plugin (@opencode-ai/plugin) to the OpenCode 2.0 plugin API (@opencode/plugin, Plugin.define + setup). Use when a plugin fails to load in OpenCode 2 with "default export must be an object with id" or "{ id, setup }", when asked to "support OpenCode V2", or when editing opencode.json plugin entries for V2.
---

# Port an OpenCode V1 plugin to V2

Source of truth: https://opencode.ai/v2/docs/build/plugins/migrate-v1/ (read it for details). V1 plugin
implementations do not run in V2. Renaming a file or a config entry is not enough; there is no automatic conversion.

## Symptoms

- `default export must be an object with id` / `{ id, setup }` in the load error: the plugin still exports a V1 function.
- The plugin is silently missing from `opencode plugin list`.
- Check status of a package: `pip install opencode-v2-ready && opencode-v2-ready check` (or https://rafal-one.github.io/opencode-v2-ready/).

## Steps

1. Dependencies: replace `@opencode-ai/plugin` with `@opencode/plugin` (depend on a version compatible with the OpenCode release you target).
2. Entrypoint: replace `export const X: Plugin = async ({ directory, project, client }) => ({ ...hooks })` with
   `export default Plugin.define({ id: "my.plugin", async setup(ctx) { ... } })`. The `id` is required and stable (storage is scoped by it).
3. Context: `directory` -> `ctx.location.directory`; `project` -> `ctx.location.project`; `client` -> domain methods (`ctx.session`, `ctx.permission`, `ctx.agent`, ...); plugin options -> `ctx.options`; Bun `$` shell -> import the process API you use.
4. Hooks: V1 returned hooks by string key; V2 registers them in `setup` on the owning domain. The callback gets one mutable `event` (not `input, output`).
5. Config: rename `plugin` to `plugins`; replace `["pkg", {opts}]` with `{ "package": "pkg", "options": {opts} }`. Local V2 files go in `.opencode/plugins/`.
6. Verify: plugin id appears in `opencode plugin list`; exercise every hook/tool/event; verify cleanup on reload; test the installed package, not a workspace link.

## Hook map (V1 -> V2, closest equivalents, not always exact renames)

| V1 | V2 |
|---|---|
| `event` | `ctx.event.subscribe()` (async iterator; abort in cleanup) |
| `dispose` | function returned by `setup` |
| `config` | transforms on the affected domains (`ctx.agent`, `ctx.command`, `ctx.mcp`, `ctx.skill`, ...) |
| `tool` (map) | `ctx.tool.transform((editor) => editor.add({ name, description, input: <JSON Schema>, execute }))`; `execute` returns `{ content }` |
| `auth` | `ctx.integration.transform(...)` and integration APIs |
| `provider` | `ctx.provider.transform(...)` and `ctx.model.transform(...)` |
| `chat.message` | `ctx.session.hook("prompt", ...)` |
| `chat.params` | `ctx.session.hook("context", ...)` (set `event.options.temperature`, `event.options.maxTokens`) |
| `chat.headers` | `ctx.session.hook("model.request", ...)` or `"http.request"` |
| `permission.ask` | `ctx.permission.hook("evaluate", ...)` |
| `command.execute.before` | `ctx.command.transform` (own commands) or the `prompt` hook |
| `tool.execute.before` | `ctx.tool.hook("execute.before", ...)` |
| `tool.execute.after` | `ctx.tool.hook("execute.after", ...)` |
| `shell.env` | `ctx.shell.hook("create.before", ...)` (edit `event.env`) |
| `tool.definition` | `ctx.tool.transform(...)` |
| `experimental.chat.system.transform` | `ctx.session.hook("context", ...)`, edit `event.system` |
| `experimental.chat.messages.transform` | `ctx.session.hook("context", ...)`, edit `event.messages` |
| `experimental.session.compacting` | `ctx.session.hook("compaction", ...)` |

No direct V2 hook: `experimental.compaction.autocontinue`, `experimental.provider.small_model`, `experimental.text.complete`
(re-evaluate against the session, provider, model and event APIs).

Transforms are replayable state edits: keep callbacks synchronous, cheap and free of one-time side effects; load external data
first and call the domain's `reload()` when it changes. Hook and transform registrations are disposed automatically on unload;
use the cleanup function for timers, sockets and subprocesses.

## Support V1 and V2 from one package (temporary)

```ts
import { Plugin } from "@opencode/plugin"
export default {
  ...Plugin.define({ id: "example", async setup(ctx) { /* V2 */ } }),
  async server() { return { /* V1 hooks */ } },   // OpenCode 1.18.29 and newer call server()
}
```

The two implementations stay separate; nothing is translated automatically. Remove the V1 half when your support window ends.
