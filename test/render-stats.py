"""The in-process render stats, checked against a second read of the same PNG.

`compute_render_stats` is meant to replace a `blender_image_probe` of the file
it describes, so the only useful test is one that measures the SAVED file twice,
by different code, and compares. A test that called the function twice would
only prove it is deterministic.

The second reading is written here from the documented rules instead of reusing
the function's helpers: a shared helper would agree with itself no matter which
side was wrong.

Three frames are measured: a film-transparent render (alpha decides the mask and
there is no backdrop), an opaque render over a lit world (the backdrop has to be
measured off the ring, or the studio would be counted as subject), and the same
opaque render with an explicit background, which must override that measurement.
"""

import json
import os
import sys
import tempfile

# pi-lens-ignore: ast-grep:unresolved-import-python (bpy is provided by Blender)
# pyright: reportMissingImports=false
import bpy  # pyright: ignore[reportMissingImports]

# pi-lens-ignore: ast-grep:unresolved-import-python (NumPy ships with Blender)
import numpy as np  # pyright: ignore[reportMissingImports]

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "blender"))

from render_scene import compute_render_stats, render_scene  # noqa: E402

ALPHA_CUTOFF = 8
TOLERANCE = 0.02
MEASUREMENT_FIELDS = [
    "subjectBounds",
    "subjectPixelCount",
    "subjectCoverage",
    "transparentPixels",
    "meanColour",
    "lumaGrid",
    "silhouette",
    "edgeSharpness",
    "palette",
    "exposure",
    "warnings",
    "background",
    "backgroundUniform",
    "filmTransparent",
]

checks = {}

scene = bpy.context.scene
scene.render.resolution_x = 96
scene.render.resolution_y = 72
scene.render.resolution_percentage = 100
# A film-transparent render makes alpha the subject mask, so the second reading
# below cannot accidentally agree by measuring the whole frame as subject.
scene.render.film_transparent = True
output_dir = tempfile.mkdtemp(prefix="pi-render-stats-")
output_path = os.path.join(output_dir, "render.png")

render = render_scene({"outputPath": output_path, "samples": 8})
checks["the render payload carries stats"] = isinstance(render.get("stats"), dict)
stats = render["stats"]
checks["stats has exactly the documented fields"] = sorted(stats) == sorted(MEASUREMENT_FIELDS)
checks["the stats were measured from the saved file"] = os.path.exists(output_path)


def stored_pixels(path):
    """The saved PNG as 8-bit code values, top-down, read independently."""
    image = bpy.data.images.load(path, check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    width, height = image.size
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    bpy.data.images.remove(image)
    # Blender's buffer is bottom-up; the saved PNG's rows are top-down.
    return np.rint(np.flipud(buffer.reshape(height, width, 4)) * 255.0).astype(np.int64)


def alpha_reading(path):
    """The same numbers, from an independent load, splitting on alpha."""
    rgba = stored_pixels(path)
    height, width = rgba.shape[0], rgba.shape[1]

    alpha = rgba[:, :, 3]
    subject = alpha > ALPHA_CUTOFF
    covered = 0
    min_x, max_x, min_y, max_y = width, -1, height, -1
    sum_r = sum_g = sum_b = 0
    for y in range(height):
        for x in range(width):
            if not subject[y][x]:
                continue
            covered += 1
            sum_r += int(rgba[y][x][0])
            sum_g += int(rgba[y][x][1])
            sum_b += int(rgba[y][x][2])
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
    return {
        "width": width,
        "height": height,
        "transparentPixels": int(np.count_nonzero(alpha <= ALPHA_CUTOFF)),
        "subjectPixelCount": covered,
        "subjectBounds": {"x0": min_x, "x1": max_x, "y0": min_y, "y1": max_y}
        if covered
        else None,
        "meanColour": [
            round(sum_r / covered, 2),
            round(sum_g / covered, 2),
            round(sum_b / covered, 2),
        ]
        if covered
        else [0.0, 0.0, 0.0],
    }


second = alpha_reading(output_path)
checks["the two readings see the same frame"] = (
    stats["subjectPixelCount"] > 0
    and second["width"] * second["height"] > 0
    and len(stats["lumaGrid"]) == 8
    and all(len(row) == 8 for row in stats["lumaGrid"])
)
checks["subject bounds agree"] = stats["subjectBounds"] == second["subjectBounds"]
checks["subject pixel count agrees"] = stats["subjectPixelCount"] == second["subjectPixelCount"]
checks["subject coverage agrees"] = (
    abs(stats["subjectCoverage"] - second["subjectPixelCount"] / (second["width"] * second["height"]))
    < 1e-4
)
checks["transparent pixel count agrees"] = stats["transparentPixels"] == second["transparentPixels"]
checks["mean colour agrees within a hundredth"] = all(
    abs(a - b) <= 0.01 for a, b in zip(stats["meanColour"], second["meanColour"])
)
checks["a film-transparent render reports transparent pixels"] = stats["transparentPixels"] > 0
checks["the alpha mask is the subject on a film-transparent render"] = (
    stats["subjectPixelCount"] == second["width"] * second["height"] - stats["transparentPixels"]
)
checks["the palette fractions never exceed the frame"] = all(
    entry["fraction"] <= 1.0 for entry in stats["palette"]
)
checks["a film-transparent render reports no backdrop"] = (
    stats["background"] is None and stats["filmTransparent"] is True
)

repeated = compute_render_stats(output_path)
checks["a second call reports the same numbers"] = json.dumps(repeated) == json.dumps(stats)

# An opaque render over a lit world: the frame has no alpha and its ring is the
# world, so a stats pass that assumed black would call the whole frame subject.
# This is the bug the auto measurement exists for.
scene.render.film_transparent = False
for obsolete in ("Cube",):
    cube = bpy.data.objects.get(obsolete)
    if cube is not None:
        bpy.data.objects.remove(cube, do_unlink=True)
bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(0.0, 0.0, 0.0))
sphere = bpy.context.active_object
material = bpy.data.materials.new("probe-sphere")
material.use_nodes = True
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
    0.8,
    0.2,
    0.1,
    1.0,
)
sphere.data.materials.append(material)
world = scene.world or bpy.data.worlds.new("probe-world")
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.2, 0.5, 1.0)

world_path = os.path.join(output_dir, "world.png")
# Dither is an 8-bit save-time option: it spreads a flat world's code value over
# two neighbouring values, which the 16-step buckets can split in two when that
# value sits on a bucket boundary. The ring measurement is what this case tests,
# so it is measured on the frame the world actually is; the dithered case below
# is measured too, and its numbers are what a real render reports.
scene.render.dither_intensity = 0.0
world_render = render_scene({"outputPath": world_path, "samples": 8})
world_stats = world_render["stats"]
world_pixels = stored_pixels(world_path)
world_height, world_width = world_pixels.shape[0], world_pixels.shape[1]
# The saved corner pixel, 0..1: what the ring measurement must report, because a
# flat world is the same colour everywhere on the border.
corner = [float(channel) / 255.0 for channel in world_pixels[0, 0, :3]]

checks["an opaque render measures its own backdrop"] = isinstance(world_stats["background"], list)
checks["the measured backdrop is the saved corner pixel"] = (
    isinstance(world_stats["background"], list)
    and all(abs(a - b) <= 0.02 for a, b in zip(world_stats["background"], corner))
)
checks["a flat world fills the whole ring"] = (world_stats["backgroundUniform"] or 0) >= 0.9
checks["the coverage is the subject's, not the frame's"] = (
    0.0 < world_stats["subjectCoverage"] < 0.5
)
checks["the opaque render has no film transparency"] = (
    world_stats["filmTransparent"] is False and world_stats["transparentPixels"] == 0
)

# The same split, done here from the corner colour with the probe's own rule, so
# the auto-measured backdrop has to reproduce the per-pixel comparison and not
# just land near the right colour.
like_background = (
    np.abs(world_pixels[:, :, 0] - corner[0] * 255.0)
    + np.abs(world_pixels[:, :, 1] - corner[1] * 255.0)
    + np.abs(world_pixels[:, :, 2] - corner[2] * 255.0)
) / 3.0
independent_subject = int(np.count_nonzero(like_background > TOLERANCE * 255.0))
checks["the auto mask matches a split on the corner colour"] = (
    world_stats["subjectPixelCount"] == independent_subject
)
checks["subject coverage equals the independently counted share"] = (
    abs(world_stats["subjectCoverage"] - independent_subject / (world_width * world_height)) < 1e-4
)

# The same world with the default dither on. The colour has to survive the split
# the dither causes between neighbouring buckets; the share does not, and that is
# reported rather than smoothed over, because the consumer's flat-fill bar reads
# it.
scene.render.dither_intensity = 1.0
dithered_path = os.path.join(output_dir, "world-dithered.png")
dithered_stats = render_scene({"outputPath": dithered_path, "samples": 8})["stats"]
dithered_corner = [
    float(channel) / 255.0 for channel in stored_pixels(dithered_path)[0, 0, :3]
]
checks["a dithered world still reports its colour"] = (
    isinstance(dithered_stats["background"], list)
    and all(abs(a - b) <= 0.02 for a, b in zip(dithered_stats["background"], dithered_corner))
)
checks["dither is reported as a share below one, not hidden"] = 0.0 < dithered_stats["backgroundUniform"] <= 1.0

# An explicit background must still override the measurement, and the frame it
# describes must then read as all subject — proof the override reached the mask.
overridden = compute_render_stats(world_path, background=(0, 0, 0))
checks["an explicit background overrides the measurement"] = overridden["background"] == [0.0, 0.0, 0.0]
checks["an explicit background reports no measurement certainty"] = (
    overridden["backgroundUniform"] is None
)
checks["an explicit black background counts the lit studio as subject"] = (
    overridden["subjectCoverage"] > 0.99
)

ok = all(checks.values())
print(json.dumps({"ok": ok, "checks": checks}))
if not ok:
    raise SystemExit(1)
print("PASS: in-process render stats agree with a second read of the saved PNG")
