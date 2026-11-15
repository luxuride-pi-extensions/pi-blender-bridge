# pi-blender-bridge

The Blender-side half of [pi-blender](https://github.com/luxuride-pi-extensions/pi-blender), in its own package.

`@luxuride-pi/blender` is MIT. This package is **GPL-3.0-or-later**, and that split is deliberate rather than incidental:

- These scripts run *inside* Blender and call `bpy`. They are loaded into the Blender process, so they are a combined program with Blender itself, and the Blender Foundation's position is that such scripts "have to be made available compliant to the GNU GPL as well" (see the [Blender license FAQ](https://www.blender.org/about/license/) and the [Extensions license requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html), which require `GPL-3.0-or-later` for add-ons).
- The extension's TypeScript never imports `bpy` and never links a Blender library. It spawns Blender as a child process and talks to it over JSON and files, so it is a separate program and can carry a permissive license.

Keeping both halves in one repository under one identifier only made that distinction invisible — the npm tarball advertised MIT while shipping GPL code, and GitHub read the whole repository as GPL. Two repositories make each boundary a license file.

## Contents

| Path | What it is |
| --- | --- |
| `blender/live-bridge.py` | The live-session bridge: request claims, the poll loop, handlers, and `--serve` mode for headless requests. |
| `blender/blank-bootstrap.py` | Loaded at Blender startup to install the bridge into a running UI session. |
| `blender/readonly_guard.py` | The read-only enforcement layer installed into the session. |
| `blender/warm_worker.py` | The warm-pool worker that keeps a Blender process ready between runs. |
| `blender/*.py` | The headless run scripts (`render_scene`, `scene_info`, `bake`, `export_asset`, `import_asset`, `montage`, `sculpt`, `shading`, `validation`, `fingerprint`, `module_state`, `object_detail`, `usage_guess`, …). |
| `test/*.py` | The `--background` probes the TypeScript test suite runs against a real Blender. |

Nothing here imports the extension's TypeScript: the interface is the payload JSON on the command line, the request/response files in the bridge directory, and the files these scripts write.

## How `@luxuride-pi/blender` finds it

`resolveBridgePackageDir()` in the extension's `config.ts` tries, in order:

1. `PI_BLENDER_BRIDGE_ASSETS_DIR` — the package root (the directory containing `blender/`). For a local checkout or a non-standard install.
2. Node resolution of `@luxuride-pi/blender-bridge/package.json` — the normal case, since the extension declares this package as a dependency.
3. A sibling checkout next to the extension (`../pi-blender-bridge`, `../blender-bridge`) — how the development symlinks under `~/.pi/agent/extensions/` resolve when the package is not in `node_modules`.
4. `~/.pi/agent/extensions/{blender-bridge,pi-blender-bridge}`.

If none of those exist it fails loudly, naming every candidate: a missing bridge is a broken install, not a degraded one, and pretending otherwise turns into "not launching Blender" much later.

## Development

The probes are plain `bpy` scripts, runnable standalone:

```bash
blender --background --factory-startup --python test/modeling-snippets.py
```

The TypeScript suite that drives them lives in the pi-blender repository (`npm test` there); it locates this package through the same resolution the extension uses.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). `Copyright (c) 2026 Luxuride`.

This package was extracted from `pi-blender`, whose Python half was GPL-3.0-or-later from 2026-10-20 onward and is the work of the same author.
