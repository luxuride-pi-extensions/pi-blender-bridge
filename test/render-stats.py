"""The in-process render stats, checked against a second read of the same PNG.

`compute_render_stats` is meant to replace a `blender_image_probe` of the file
it describes, so the only useful test is one that measures the SAVED file twice,
by different code, and compares. A test that called the function twice would
only prove it is deterministic.

The second reading is written here from the documented rules instead of reusing
the function's helpers: a shared helper would agree with itself no matter which
side was wrong.
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
checks["stats has exactly the probe's measurement fields"] = sorted(stats) == sorted(
    [
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
    ]
)
checks["the stats were measured from the saved file"] = os.path.exists(output_path)


def second_reading(path):
    """The same numbers, from an independent load of the saved PNG."""
    image = bpy.data.images.load(path, check_existing=False)
    image.colorspace_settings.name = "Non-Color"
    width, height = image.size
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    bpy.data.images.remove(image)
    # Blender's buffer is bottom-up; the saved PNG's rows are top-down.
    rgba = np.rint(np.flipud(buffer.reshape(height, width, 4)) * 255.0).astype(np.int64)

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


second = second_reading(output_path)
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

repeated = compute_render_stats(output_path)
checks["a second call reports the same numbers"] = json.dumps(repeated) == json.dumps(stats)

ok = all(checks.values())
print(json.dumps({"ok": ok, "checks": checks}))
if not ok:
    raise SystemExit(1)
print("PASS: in-process render stats agree with a second read of the saved PNG")
