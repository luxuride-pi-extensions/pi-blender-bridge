"""One scene-render implementation for every headless Blender path.

`blender_render` and a module's branch run both produce a camera render, and
they used to do it with two scripts. The branch renderer never chose an engine
at all, so the engine a branch image came from was unrecorded, and the render
tool's preference list was silently dropped when assigning it raised. Both now
call `render_scene`, so "which engine did that render use" has one answer and a
preference list is actually walked.

Imported by generated Blender scripts, not by the extension: everything here
needs a live `bpy`.
"""

import math
import os

# pi-lens-ignore: ast-grep:unresolved-import-python (bpy is provided by Blender's Python runtime)
import bpy  # pyright: ignore[reportMissingImports]


def set_engine(scene, desired):
    """Select the first usable engine from a name or a preference list.

    `desired` may be a single identifier or an ordered list of candidates
    (`mapBackgroundRenderEngine` returns a list for version-dependent names).
    Returns the engine the scene ended up with; raises when nothing usable
    exists rather than silently rendering with whichever engine was current.
    """
    if not desired:
        return scene.render.engine

    preferences = [desired] if isinstance(desired, str) else list(desired)
    for candidate in preferences:
        try:
            scene.render.engine = candidate
        except (TypeError, ValueError):
            continue
        return scene.render.engine

    try:
        available = {
            item.identifier
            for item in scene.render.bl_rna.properties["engine"].enum_items
        }
    except (AttributeError, KeyError, TypeError):
        available = set()
    # Workbench is deliberately absent: it is a flat, unlit viewport mode, and as
    # a last resort it would replace a requested Cycles/EEVEE image with one no
    # critic can judge. An explicit `workbench` request still reaches it above.
    for candidate in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES"):
        if candidate in available:
            scene.render.engine = candidate
            return scene.render.engine
    raise RuntimeError(
        f'No usable render engine for "{desired}"; this build offers: '
        + (", ".join(sorted(available)) or "nothing enumerable")
    )


# Perceived-luminance weights and the alpha cutoff, fixed here to the same
# values `blender/image-probe.ts` uses: the two implementations are compared
# field by field, so a constant that drifts on one side is a silent disagreement.
_LUMA_R = 0.2126
_LUMA_G = 0.7152
_LUMA_B = 0.0722
_ALPHA_CUTOFF = 8
_MAX_GRID_SIDE = 32
_MAX_PALETTE_LIMIT = 16
_PALETTE_STEPS = 15
_EXPOSURE_SAMPLE_CAP = 8192


def _round(value, digits):
    """Round half-up like JavaScript's `Math.round`, not Python's banker's."""
    factor = 10.0**digits
    return math.floor(value * factor + 0.5) / factor


def _js_number(value, digits):
    """A rounded number as JavaScript would print it, for identical warnings."""
    text = f"{_round(value, digits):.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _stored_pixels(path):
    """The saved PNG's 8-bit code values, top-down, as one int array.

    Read back with the `Non-Color` colorspace because the sRGB default returns
    scene-linear floats: the numbers this has to agree with are measured on the
    8-bit sRGB code values a critic's viewer shows, which is also what
    `blender/image-probe.ts` decodes out of the same file. Blender's buffer is
    bottom-up, so it is flipped to the top-down order the probe's rows use.
    """
    # pi-lens-ignore: ast-grep:unresolved-import-python (NumPy ships with Blender)
    import numpy as np  # pyright: ignore[reportMissingImports]

    image = bpy.data.images.load(path, check_existing=False)
    try:
        image.colorspace_settings.name = "Non-Color"
        width, height = image.size
        flat = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(flat)
    except (OSError, RuntimeError, AttributeError) as exc:
        raise RuntimeError(f"could not read {path} back for measurement: {exc}") from exc
    finally:
        bpy.data.images.remove(image)
    return np.rint(np.flipud(flat.reshape(height, width, 4)) * 255.0).astype(np.int64)


def compute_render_stats(
    output_path, background=(0, 0, 0), grid=(8, 8), palette_limit=6, tolerance=0.02
):
    """Measure the PNG this run just saved, field for field like the image probe.

    These numbers are computed from the SAVED PNG, inside the render process,
    with the same rules `blender/image-probe.ts` applies: the 8-bit sRGB code
    values of `output_path` are the whole input, so a fresh `blender_image_probe`
    of that same file reports the same numbers. They are NOT measurements of the
    scene-linear render buffer, and they are not expected to agree with one —
    so do not diff these against a probe of some other file and conclude the
    probe is broken.

    The field set is the measurement half of `ImageProbeResult`, so a caller can
    substitute this for a probe of the file it describes: `subjectBounds`,
    `subjectPixelCount`, `subjectCoverage`, `transparentPixels`, `meanColour`,
    `lumaGrid`, `silhouette`, `edgeSharpness`, `palette`, `exposure` and
    `warnings`.
    """
    # pi-lens-ignore: ast-grep:unresolved-import-python (NumPy ships with Blender)
    import numpy as np  # pyright: ignore[reportMissingImports]

    rgba = _stored_pixels(output_path)
    height, width = rgba.shape[0], rgba.shape[1]
    total = width * height
    red = rgba[:, :, 0].astype(np.float64)
    green = rgba[:, :, 1].astype(np.float64)
    blue = rgba[:, :, 2].astype(np.float64)
    alpha = rgba[:, :, 3]

    nx = min(_MAX_GRID_SIDE, max(1, int(round(float(grid[0])))))
    ny = min(_MAX_GRID_SIDE, max(1, int(round(float(grid[1])))))
    limit = min(_MAX_PALETTE_LIMIT, max(0, int(round(float(palette_limit)))))
    tolerance = max(0.0, min(1.0, float(tolerance)))

    transparent = alpha <= _ALPHA_CUTOFF
    transparent_pixels = int(np.count_nonzero(transparent))
    backdrop = [float(channel) * 255.0 for channel in background]
    # Mean per-channel distance, the same bar the probe uses: `tolerance` reads
    # as "how far a pixel may sit from the background and still be background".
    # A negative threshold disables the comparison, which is how a film-
    # transparent render makes alpha the only thing that decides.
    distance = (np.abs(red - backdrop[0]) + np.abs(green - backdrop[1]) + np.abs(blue - backdrop[2])) / 3.0
    threshold = -1.0 if transparent_pixels > 0 else tolerance * 255.0
    mask = (~transparent) & (distance > threshold)
    subject_pixels = int(np.count_nonzero(mask))

    warnings = []
    backdrop_text = "[" + ",".join(_js_number(channel, 6) for channel in background) + "]"
    if subject_pixels == 0:
        warnings.append(
            f"No pixel differs from the background {backdrop_text} by more than the tolerance, "
            "so every measurement is empty. The background colour is probably wrong for this "
            "image — read it from the render settings rather than re-probing with another guess."
        )
    if transparent_pixels > 0:
        warnings.append(
            f"{transparent_pixels} of {total} pixels are transparent and were counted as subject. "
            "On a film-transparent render the alpha channel is the subject mask and the "
            "background colour is irrelevant."
        )

    luma = (_LUMA_R * red + _LUMA_G * green + _LUMA_B * blue) / 255.0

    if subject_pixels > 0:
        rows = np.flatnonzero(mask.any(axis=1))
        columns = np.flatnonzero(mask.any(axis=0))
        subject_bounds = {
            "x0": int(columns[0]),
            "x1": int(columns[-1]),
            "y0": int(rows[0]),
            "y1": int(rows[-1]),
        }
    else:
        subject_bounds = None
    divisor = subject_pixels if subject_pixels > 0 else 1
    mean_colour = [
        _round(float(red[mask].sum()) / divisor, 2),
        _round(float(green[mask].sum()) / divisor, 2),
        _round(float(blue[mask].sum()) / divisor, 2),
    ]

    # Composition grid: cell (x, y) is the mean luma of its subject pixels, or
    # null where the cell holds none, so an empty cell is visibly empty rather
    # than a zero that reads as black.
    cell_x = np.minimum(nx - 1, (np.arange(width) * nx) // width)
    cell_y = np.minimum(ny - 1, (np.arange(height) * ny) // height)
    grid_cells = np.zeros((ny, nx), dtype=np.float64)
    grid_counts = np.zeros((ny, nx), dtype=np.int64)
    rows_index, columns_index = np.nonzero(mask)
    np.add.at(grid_cells, (cell_y[rows_index], cell_x[columns_index]), luma[mask])
    np.add.at(grid_counts, (cell_y[rows_index], cell_x[columns_index]), 1)
    luma_grid = [
        [
            _round(float(grid_cells[row][column]) / int(grid_counts[row][column]), 4)
            if grid_counts[row][column] > 0
            else None
            for column in range(nx)
        ]
        for row in range(ny)
    ]

    per_row = np.count_nonzero(mask, axis=1)
    coverage_by_row = []
    run_start = None
    run_widest = 0
    for row in range(height + 1):
        count = int(per_row[row]) if row < height else 0
        if count > 0:
            if run_start is None:
                run_start = row
            run_widest = max(run_widest, count)
            continue
        if run_start is not None:
            coverage_by_row.append({"from": run_start, "to": row - 1, "maxWidth": run_widest})
            run_start = None
            run_widest = 0
    silhouette = {
        "rowsOccupied": int(np.count_nonzero(per_row)),
        "rowCount": height,
        "widestRow": int(per_row.max()) if height > 0 else 0,
        "coverageByRow": coverage_by_row,
    }

    # Edge sharpness: only the pixels that straddle the silhouette count. An
    # interior step says nothing about the edge, and averaging those in would
    # report a hard-edged subject as blurry because most of it is interior.
    here = mask[:, :-1]
    there = mask[:, 1:]
    boundary = here != there
    right_alpha = alpha[:, 1:]
    boundary = boundary & (right_alpha > _ALPHA_CUTOFF)
    crossings = int(np.count_nonzero(boundary))
    if crossings == 0:
        edge_sharpness = None
    else:
        steps = np.abs(luma[:, :-1] - luma[:, 1:])[boundary]
        edge_sharpness = _round(float(steps.sum()) / crossings, 5)

    palette = []
    if limit > 0 and subject_pixels > 0:
        quantised = (
            (np.rint(red[mask] / 255.0 * _PALETTE_STEPS).astype(np.int64) << 8)
            | (np.rint(green[mask] / 255.0 * _PALETTE_STEPS).astype(np.int64) << 4)
            | np.rint(blue[mask] / 255.0 * _PALETTE_STEPS).astype(np.int64)
        )
        keys, first_seen, counts = np.unique(quantised, return_index=True, return_counts=True)
        # Ties break on first appearance, which is what the probe's insertion-
        # ordered Map plus a stable sort does.
        order = np.lexsort((first_seen, -counts))[:limit]
        step = 255.0 / _PALETTE_STEPS
        palette = [
            {
                "hex": "#%02x%02x%02x"
                % (
                    int(round(((int(keys[index]) >> 8) & 15) * step)),
                    int(round(((int(keys[index]) >> 4) & 15) * step)),
                    int(round((int(keys[index]) & 15) * step)),
                ),
                "fraction": _round(float(counts[index]) / subject_pixels, 4),
            }
            for index in order
        ]

    # Exposure over the subject, on the probe's deterministic stride sample:
    # whole-image clip fractions would count the backdrop, and a white cyclorama
    # is 100% clipped while saying nothing about the subject.
    stride = max(1, subject_pixels // _EXPOSURE_SAMPLE_CAP)
    sampled_mask = np.zeros_like(mask)
    if subject_pixels > 0 and stride == 1:
        sampled_mask = mask
    elif subject_pixels > 0:
        positions = np.flatnonzero(mask)
        sampled_mask.flat[positions[stride - 1 :: stride]] = True
    sample_red = red[sampled_mask]
    sample_green = green[sampled_mask]
    sample_blue = blue[sampled_mask]
    sampled_pixels = int(sample_red.size)
    sampled_divisor = sampled_pixels if sampled_pixels > 0 else 1
    sample_lumas = np.sort(luma[sampled_mask])

    def percentile(fraction):
        if sample_lumas.size == 0:
            return 0
        index = min(sample_lumas.size - 1, max(0, math.floor((sample_lumas.size - 1) * fraction + 0.5)))
        return _round(float(sample_lumas[index]), 4)

    def fraction(count):
        return _round(float(count) / sampled_divisor, 5)

    high_r = sample_red >= 255
    high_g = sample_green >= 255
    high_b = sample_blue >= 255
    low_r = sample_red <= 0
    low_g = sample_green <= 0
    low_b = sample_blue <= 0

    exposure = {
        "sampled": stride > 1,
        "sampleCount": sampled_pixels,
        "clippedHigh": {
            "r": fraction(np.count_nonzero(high_r)),
            "g": fraction(np.count_nonzero(high_g)),
            "b": fraction(np.count_nonzero(high_b)),
            "any": fraction(np.count_nonzero(high_r | high_g | high_b)),
            "all": fraction(np.count_nonzero(high_r & high_g & high_b)),
        },
        "clippedLow": {
            "r": fraction(np.count_nonzero(low_r)),
            "g": fraction(np.count_nonzero(low_g)),
            "b": fraction(np.count_nonzero(low_b)),
            "any": fraction(np.count_nonzero(low_r | low_g | low_b)),
            "all": fraction(np.count_nonzero(low_r & low_g & low_b)),
        },
        "lumaPercentiles": {"p1": percentile(0.01), "p50": percentile(0.5), "p99": percentile(0.99)},
    }

    clipped_high = _round(exposure["clippedHigh"]["all"] * 100, 1)
    clipped_low = _round(exposure["clippedLow"]["all"] * 100, 1)
    over_clipped = _round((clipped_high + clipped_low) / 100, 4)
    luma_range = exposure["lumaPercentiles"]["p99"] - exposure["lumaPercentiles"]["p1"]
    # A uniformly white or black subject is flat because it is clipped, and the
    # clipped warning below says so; calling that flat LIGHTING would tell a
    # critic to relight a legitimately white object.
    if sampled_pixels > 0 and luma_range < 0.05 and over_clipped <= 0.02:
        warnings.append(
            f"The subject's luma spans only {_js_number(luma_range, 3)} from p1 to p99. "
            "If the subject is not a single flat material, this is a flat-light render: a "
            "shadow, a highlight and a material change are all nearly the same value, so a "
            "critic cannot judge shape or material until the lighting changes."
        )
    if sampled_pixels > 0 and over_clipped > 0.02:
        warnings.append(
            f"{_js_number(over_clipped * 100, 1)}% of the {sampled_pixels} sampled subject pixels "
            f"are clipped ({_js_number(clipped_high, 1)}% white on all channels, "
            f"{_js_number(clipped_low, 1)}% black). If the subject is not itself pure white or "
            "black, this is an exposure or lighting problem and detail there cannot be judged "
            "from this render."
        )

    return {
        "subjectBounds": subject_bounds,
        "subjectPixelCount": subject_pixels,
        "subjectCoverage": _round(subject_pixels / total, 5),
        "transparentPixels": transparent_pixels,
        "meanColour": mean_colour,
        "lumaGrid": luma_grid,
        "silhouette": silhouette,
        "edgeSharpness": edge_sharpness,
        "palette": palette,
        "exposure": exposure,
        "warnings": warnings,
    }


def render_scene(payload):
    """Render the current scene, then describe what was rendered.

    The camera and the engine are the two settings that make a render
    comparable between callers, so both are reported back: `resolvedEngine` is
    what the build actually used, and `requestedEngine` is what the caller asked
    for (None when it deferred to the scene).
    """
    scene = bpy.context.scene
    requested_engine = payload.get("renderEngine")
    resolved_engine = set_engine(scene, requested_engine)

    camera_name = payload.get("cameraName")
    if camera_name:
        camera_object = bpy.data.objects.get(camera_name)
        if camera_object is None:
            raise RuntimeError(f"Camera not found: {camera_name}")
        scene.camera = camera_object

    if scene.camera is None:
        fallback = bpy.data.objects.get("Camera")
        if fallback is not None:
            scene.camera = fallback

    if scene.camera is None:
        raise RuntimeError("No camera available for rendering")

    resolution = payload.get("resolution") or {}
    scene.render.resolution_x = int(resolution.get("x", scene.render.resolution_x))
    scene.render.resolution_y = int(resolution.get("y", scene.render.resolution_y))
    scene.render.resolution_percentage = int(
        resolution.get("percentage", scene.render.resolution_percentage)
    )

    samples = payload.get("samples")
    if samples is not None and hasattr(scene, "cycles"):
        scene.cycles.samples = int(samples)

    output_path = payload["outputPath"]
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"could not create the render directory: {exc}") from exc
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = output_path
    bpy.ops.render.render(write_still=True)

    # Measured from the file that was just written, so a critic's numbers and a
    # critic's picture are the same bytes. The defaults are the probe's own: a
    # film-transparent render states its subject through alpha, and a guessed
    # backdrop is not passed in from here.
    stats = compute_render_stats(output_path)

    return {
        "outputPath": output_path,
        "cameraName": scene.camera.name if scene.camera else None,
        "resolvedEngine": resolved_engine,
        "requestedEngine": requested_engine,
        "resolution": {
            "x": int(scene.render.resolution_x),
            "y": int(scene.render.resolution_y),
            "percentage": int(scene.render.resolution_percentage),
        },
        "stats": stats,
    }

# A 3x5 font drawn with NumPy. Sheet labels have to survive headless Blender,
# which has no reliable text API before a render, and a critic must not need the
# JSON sidecar to know which panel it is looking at.
_FONT = {
    "A": ("111", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("111", "101", "110", "101", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("000", "000", "000", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
}


def _draw_label(pixels, text, scale=2):
    """Draw an uppercase label into the top strip of a panel."""
    height, width, _channels = pixels.shape
    strip = min(height, 6 * scale + 6)
    # Blender image buffers are bottom-up, so the visual top strip is the last
    # rows of the array. Drawing into rows 0..strip would put the label at the
    # bottom of the saved PNG.
    top_row = height - strip
    pixels[top_row:height, :, :3] = 0.02
    x = 6
    for character in str(text).upper():
        glyph = _FONT.get(character)
        if glyph is None:
            x += 4 * scale
            continue
        for row, line in enumerate(glyph):
            for column, bit in enumerate(line):
                if bit != "1":
                    continue
                bottom = top_row + 3 + (4 - row) * scale
                left = x + column * scale
                if bottom + scale > height or left + scale > width:
                    continue
                pixels[bottom : bottom + scale, left : left + scale, :3] = 0.95
        x += 4 * scale
        if x >= width - 6:
            break


def compose_review_sheet(panels, output_path, columns=2, gutter=8):
    """Tile rendered panels into one sheet without leaving Blender.

    NumPy and `bpy.data.images` ship with Blender, so this needs no dependency
    the extension does not already have. Panels are loaded as scene-linear
    pixels and written back through an sRGB image, which is the round trip a
    saved PNG takes; reading and writing the same buffer keeps the colours the
    renders had. Each panel carries a drawn label: the tool result also names
    the panels, but a critic that reads only the image must still know which
    generation and shot it is looking at. A panel that cannot be read is
    reported by name, not silently replaced with a blank tile.
    """
    # pi-lens-ignore: ast-grep:unresolved-import-python (NumPy ships with Blender)
    import numpy as np  # pyright: ignore[reportMissingImports]

    if not panels:
        raise RuntimeError("a review sheet needs at least one panel")
    loaded = []
    try:
        for panel in panels:
            path = panel["file"]
            image = bpy.data.images.load(path, check_existing=False)
            pixels = np.array(image.pixels[:], dtype=np.float32).reshape(
                image.size[1], image.size[0], 4
            )
            loaded.append((panel, pixels))
            bpy.data.images.remove(image)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"could not read a review panel: {error}") from error

    panel_width, panel_height = 640, 480
    resized = []
    for panel, pixels in loaded:
        # Aspect is preserved by fitting inside the tile, never by stretching:
        # a square or portrait hero squeezed into 4:3 would read as geometry.
        scale = min(panel_width / pixels.shape[1], panel_height / pixels.shape[0])
        target_width = max(1, int(pixels.shape[1] * scale))
        target_height = max(1, int(pixels.shape[0] * scale))
        if target_width != pixels.shape[1] or target_height != pixels.shape[0]:
            row_index = np.linspace(0, pixels.shape[0] - 1, target_height).astype(np.int32)
            column_index = np.linspace(0, pixels.shape[1] - 1, target_width).astype(np.int32)
            pixels = pixels[np.ix_(row_index, column_index)]
        tile = np.full((panel_height, panel_width, 4), 0.06, dtype=np.float32)
        tile[:, :, 3] = 1.0
        top = (panel_height - target_height) // 2
        left = (panel_width - target_width) // 2
        tile[top : top + target_height, left : left + target_width] = pixels
        _draw_label(tile, panel.get("label") or panel.get("name") or "")
        resized.append((panel, tile))
    loaded = resized
    rows = (len(loaded) + columns - 1) // columns
    sheet_width = columns * panel_width + (columns + 1) * gutter
    sheet_height = rows * panel_height + (rows + 1) * gutter
    sheet = np.full((sheet_height, sheet_width, 4), 0.06, dtype=np.float32)
    sheet[:, :, 3] = 1.0
    for index, (_panel, pixels) in enumerate(loaded):
        column = index % columns
        row = index // columns
        x = gutter + column * (panel_width + gutter)
        y_from_top = gutter + row * (panel_height + gutter)
        y = sheet_height - y_from_top - panel_height
        sheet[y : y + panel_height, x : x + panel_width] = pixels

    image = bpy.data.images.new(
        "pi_review_sheet", width=sheet_width, height=sheet_height, alpha=True
    )
    image.colorspace_settings.name = "sRGB"
    image.pixels.foreach_set(sheet.ravel())
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"could not create the review-sheet directory: {exc}") from exc
    image.filepath_raw = output_path
    image.file_format = "PNG"
    image.save()
    bpy.data.images.remove(image)
    return {"path": output_path, "width": sheet_width, "height": sheet_height}

