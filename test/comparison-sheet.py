#!/usr/bin/env python3
"""Exercise the comparison sheet's difference panel against known pixels.

Two renders that differ by a moved block are written as real PNGs, composed
through the same loader a branch run uses, and read back: every assertion below
is about a value in the returned diff or in a pixel of the sheet, which is a
fact about the code rather than about a render.

Run: blender --background --python test/comparison-sheet.py
Exit code 0 means every assertion held.
"""

import os
import shutil
import sys
import tempfile

import bpy

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blender")
)

import render_scene  # noqa: E402

import numpy  # noqa: E402

failures = []

PANEL_WIDTH, PANEL_HEIGHT = 640, 480
TILE_WIDTH, TILE_HEIGHT = render_scene.TILE_WIDTH, render_scene.TILE_HEIGHT
GUTTER = 8


def check(condition, message):
    if not condition:
        failures.append(message)
    print(f"  {'ok  ' if condition else 'FAIL'}: {message}")
    sys.stdout.flush()


def solid(colour, width=PANEL_WIDTH, height=PANEL_HEIGHT):
    panel = numpy.zeros((height, width, 4), dtype=numpy.float32)
    panel[:, :] = (colour[0], colour[1], colour[2], 1.0)
    return panel


def with_block(columns):
    """A dark panel carrying one bright block at the named column range."""
    panel = solid((0.1, 0.1, 0.1))
    panel[100:160, columns[0] : columns[1], :3] = 0.8
    return panel


def write_png(path, pixels):
    image = bpy.data.images.new(
        "pi_test_panel", width=pixels.shape[1], height=pixels.shape[0], alpha=True
    )
    image.colorspace_settings.name = "sRGB"
    image.pixels.foreach_set(pixels.ravel())
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)


def read_png(path):
    image = bpy.data.images.load(path, check_existing=False)
    pixels = numpy.array(image.pixels[:], dtype=numpy.float32).reshape(
        image.size[1], image.size[0], 4
    )
    bpy.data.images.remove(image)
    return pixels


def panel(name, label, path):
    return {"name": name, "label": label, "file": path}


def sheet_region(sheet, column):
    """The (rows, columns) slice of one full-tile panel in a one-row sheet."""
    left = GUTTER + column * (TILE_WIDTH + GUTTER)
    top = GUTTER
    bottom = sheet.shape[0] - top - TILE_HEIGHT
    return sheet[bottom : bottom + TILE_HEIGHT, left : left + TILE_WIDTH, :3]


def tile_width(count):
    return count * TILE_WIDTH + (count + 1) * GUTTER


# Blender under flatpak cannot see /tmp, so the scratch files live where the
# process can read them back.
scratch = tempfile.mkdtemp(prefix="pi-comparison-", dir=os.path.expanduser("~"))
try:
    base_path = os.path.join(scratch, "base.png")
    same_path = os.path.join(scratch, "same.png")
    moved_path = os.path.join(scratch, "moved.png")
    small_path = os.path.join(scratch, "small.png")
    write_png(base_path, with_block((100, 160)))
    write_png(same_path, with_block((100, 160)))
    write_png(moved_path, with_block((300, 360)))
    write_png(small_path, solid((0.1, 0.1, 0.1), width=PANEL_WIDTH // 2, height=PANEL_HEIGHT // 2))

    # --- identical generations report no change and no diff panel ---------
    identical = render_scene.compose_comparison_sheet(
        [
            panel("current", "CURRENT - ITERATION 2", same_path),
            panel("previous 1", "PREVIOUS - ITERATION 1", base_path),
        ],
        os.path.join(scratch, "identical.png"),
    )
    diff = identical["diff"]
    check(diff is not None, "identical panels produce diff metrics")
    check(diff["meanAbsDiff"] == 0.0, f"identical panels differ by 0: {diff['meanAbsDiff']}")
    check(diff["maxAbsDiff"] == 0.0, f"identical panels have max 0: {diff['maxAbsDiff']}")
    check(diff["structuralScore"] == 1.0, f"structuralScore is 1.0 when identical: {diff['structuralScore']}")
    check(diff["changedPixelRatio"] == 0.0, f"no pixel changed: {diff['changedPixelRatio']}")
    check(diff["widthMatch"] is True, "identical panels match in size")
    check(diff["warnings"] == [], f"identical panels warn about nothing: {diff['warnings']}")
    check(
        identical["width"] == tile_width(2),
        f"no DIFF panel is added when nothing differs: width {identical['width']}",
    )
    check(
        identical["height"] == TILE_HEIGHT + 2 * GUTTER,
        f"the sheet is one tile tall: {identical['height']}",
    )

    # --- a review sheet's contract is untouched ---------------------------
    review = render_scene.compose_review_sheet(
        [
            panel("current", "CURRENT - ITERATION 2", same_path),
            panel("previous 1", "PREVIOUS - ITERATION 1", base_path),
        ],
        os.path.join(scratch, "review.png"),
    )
    check(
        sorted(review) == ["height", "path", "width"],
        f"compose_review_sheet still returns only path, width and height: {sorted(review)}",
    )
    check(review["width"] == tile_width(2), f"review sheet keeps its tile count: {review['width']}")

    # --- a moved block is measured and drawn ------------------------------
    moved = render_scene.compose_comparison_sheet(
        [
            panel("current", "CURRENT - ITERATION 2", moved_path),
            panel("previous 1", "PREVIOUS - ITERATION 1", base_path),
        ],
        os.path.join(scratch, "moved-sheet.png"),
    )
    moved_diff = moved["diff"]
    check(moved_diff["meanAbsDiff"] > 0.0, f"a moved block shows a nonzero mean: {moved_diff['meanAbsDiff']}")
    check(
        moved_diff["structuralScore"] < 1.0,
        f"structuralScore drops below 1.0: {moved_diff['structuralScore']}",
    )
    check(
        moved_diff["changedPixelRatio"] > 0.0,
        f"a moved block changes pixels: {moved_diff['changedPixelRatio']}",
    )
    check(
        abs(moved_diff["meanAbsDiff"] - (1.0 - moved_diff["structuralScore"])) < 1e-6,
        "structuralScore is 1.0 - meanAbsDiff",
    )
    check(
        moved["width"] == tile_width(3),
        f"the DIFF panel is inserted as a third panel: width {moved['width']}",
    )
    sheet = read_png(moved["path"])
    diff_panel = sheet_region(sheet, 2)
    heat_at_old_block = float(diff_panel[130, 130].max())
    heat_at_new_block = float(diff_panel[130, 330].max())
    heat_at_quiet_corner = float(diff_panel[400, 600].max())
    check(heat_at_old_block > 0.5, f"the vacated block position is hot: {heat_at_old_block:.3f}")
    check(heat_at_new_block > 0.5, f"the new block position is hot: {heat_at_new_block:.3f}")
    check(
        abs(heat_at_quiet_corner - 0.06) < 0.02,
        f"unchanged pixels stay at the sheet background: {heat_at_quiet_corner:.3f}",
    )
    label_band = diff_panel[TILE_HEIGHT - 18 : TILE_HEIGHT, :, :]
    check(
        float(label_band.min()) < 0.2 and float(label_band.max()) > 0.5,
        "the DIFF panel carries a legible label in its top band",
    )

    # --- a reference keeps the diff between previous and reference ---------
    with_reference = render_scene.compose_comparison_sheet(
        [
            panel("current", "CURRENT - ITERATION 2", moved_path),
            panel("previous 1", "PREVIOUS - ITERATION 1", base_path),
            panel("reference", "REFERENCE - NOT A GENERATION", same_path),
        ],
        os.path.join(scratch, "with-reference.png"),
    )
    check(
        with_reference["width"] == tile_width(4),
        f"a reference keeps four panels, DIFF before it: width {with_reference['width']}",
    )
    labels_sheet = read_png(with_reference["path"])
    reference_panel = sheet_region(labels_sheet, 3)
    check(
        float(reference_panel[130, 330].max()) < 0.2,
        f"the reference panel is the untouched generation, not the heatmap: {float(reference_panel[130, 330].max()):.3f}",
    )

    # --- mismatched dimensions degrade to the side-by-side sheet ----------
    mismatched = render_scene.compose_comparison_sheet(
        [
            panel("current", "CURRENT - ITERATION 2", base_path),
            panel("previous 1", "PREVIOUS - ITERATION 1", small_path),
        ],
        os.path.join(scratch, "mismatched.png"),
    )
    mismatched_diff = mismatched["diff"]
    check(mismatched_diff["widthMatch"] is False, "a size mismatch reports widthMatch false")
    check(
        mismatched_diff["warnings"] == [render_scene.COMPARISON_SIZE_WARNING],
        f"a size mismatch names the reason: {mismatched_diff['warnings']}",
    )
    check(mismatched_diff["meanAbsDiff"] is None, "no number is invented for incomparable panels")
    check(
        mismatched["width"] == tile_width(2),
        f"a size mismatch keeps the side-by-side sheet: width {mismatched['width']}",
    )
finally:
    shutil.rmtree(scratch, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} assertion(s) failed:")
    for message in failures:
        print(f"  - {message}")
    sys.exit(1)
print("PASS: comparison sheets report their difference and degrade honestly")
