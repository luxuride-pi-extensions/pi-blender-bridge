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
| `blender/generate-calibration-fixtures.py` | Renders the critic calibration fixture set -- eight self-authored primitives, three clean and five defective -- that pi-blender's critic tests score. It lives here because it imports `bpy` and therefore carries this package's licence; the images it writes are committed in pi-blender as data. |
| `blender/binder/*.py` | The FBX/OBJ transfer templates `@luxuride-pi/unity-blender` stages with a baked configuration blob (Unity <-> Blender moves have no custom-argument channel, so the settings are written into a copy of the script). |

Nothing here imports the extension's TypeScript: the interface is the payload JSON on the command line, the request/response files in the bridge directory, and the files these scripts write.

## How `@luxuride-pi/blender` finds it

`resolveBridgePackageDir()` in the extension's `config.ts` tries, in order:

1. `PI_BLENDER_BRIDGE_ASSETS_DIR` — the package root (the directory containing `blender/`). For a local checkout or a non-standard install.
2. Node resolution of `@luxuride-pi/blender-bridge/package.json` — when it sits in `node_modules` (an explicit install, or a package manager that permits non-registry artifacts).
3. A sibling checkout next to the extension (`../pi-blender-bridge`, `../blender-bridge`) — how the development symlinks under `~/.pi/agent/extensions/` resolve when the package is not in `node_modules`.
4. `~/.pi/agent/extensions/{blender-bridge,pi-blender-bridge}`.

If none of those exist it fails loudly, naming every candidate: a missing bridge is a broken install, not a degraded one, and pretending otherwise turns into "not launching Blender" much later.

`@luxuride-pi/unity-blender` resolves `blender/binder/` through the same environment
variable and the same order, so one bridge installation serves both extensions.

### Why this is not an npm dependency

npm 12 ships `allow-git` and `allow-remote` set to `none`, so a dependency
written as a git spec or as a release-asset URL fails with

```
npm error Fetching packages of type "git" have been disabled
```

for every consumer. A registry dependency would work, but the package is not
published there on purpose: it is GPL code that exists to be installed beside a
Blender install, not a library to link. So it is a prerequisite — a clone beside
the extension, or `PI_BLENDER_BRIDGE_ASSETS_DIR` — and the extension's error
message names the clone command rather than pretending an install will fetch it.

## Development

The probes are plain `bpy` scripts, runnable standalone:

```bash
blender --background --factory-startup --python test/modeling-snippets.py
```

The TypeScript suite that drives them lives in the pi-blender repository (`npm test` there); it locates this package through the same resolution the extension uses.

The calibration fixture generator is run the same way, from the pi-blender checkout, because that is where its output is committed:

```bash
cd ../pi-blender
HOME=$HOME flatpak run --command=blender org.blender.Blender --background --factory-startup \
    --python /abs/path/to/pi-blender-bridge/blender/generate-calibration-fixtures.py
```

It rewrites `test/fixtures/critic-calibration/manifest.json` from the labels in its `CASES` table and the hashes it measures. A Blender upgrade changes the sampler and therefore the hashes, so regenerating is a normal part of that upgrade, not a repair.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). `Copyright (c) 2026 Luxuride`.

This package was extracted from `pi-blender`, whose Python half was GPL-3.0-or-later from 2026-10-20 onward and is the work of the same author.
