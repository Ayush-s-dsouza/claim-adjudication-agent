"""Generates the Phase 1 eval set: single-field extraction cases with known
ground truth, used to diagnose what Sarvam's Extract confidence score
actually tracks (see README's "Confidence diagnosis" section).

Design (see the README for the full rationale):
- 5 field types x 4 ground-truth values x 5 degradation levels = 100 cases.
  100 is the closest count to the requested "~120, ~24/level" that is both
  evenly divisible by 25 (5 types x 5 levels, for exact per-cell balance)
  and evenly divisible by 10 (for a clean 40/30/30 split) -- 120 itself
  divides into neither cleanly.
- Field types: typeset (generic printed text), name, numeric, date,
  handwriting (same content, rendered in a script font). This is a
  rendering-style axis, independent of degradation.
- Degradation levels: clean, mild, heavy, illegible, absent. This is a
  legibility axis, independent of field type. "illegible" degrades only the
  value's own region (same technique as the place_of_death test in
  samples/); "absent" simply never draws the value.
- Every (field_type, value) pair is rendered once at each of the 5
  degradation levels, so the same ground-truth value is traceable across
  the full legibility range.

Split: cases are pooled (already perfectly balanced, 4 per field_type x
degradation_level cell) and assigned to tune/validation/test with a fixed
seed for reproducibility -- see split_cases() below. Load splits via
eval/splits.py, not by reading manifest.json directly, so the test-set lock
in that file is actually enforced.

    python eval/generate_cases.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).parent
CASES_DIR = OUT_DIR / "cases"
MANIFEST_PATH = OUT_DIR / "manifest.json"

CANVAS_SIZE = (900, 220)
PAPER = (250, 248, 240)
INK = (15, 15, 15)
HAND_INK = (25, 35, 130)

FONTS_DIR = Path(r"C:\Windows\Fonts")
label_font = ImageFont.truetype(str(FONTS_DIR / "arial.ttf"), 22)
value_font = ImageFont.truetype(str(FONTS_DIR / "arial.ttf"), 26)
hand_font = ImageFont.truetype(str(FONTS_DIR / "segoepr.ttf"), 28)

DEGRADATION_LEVELS = ["clean", "mild", "heavy", "illegible", "absent"]
FIELD_TYPES = ["typeset", "name", "numeric", "date", "handwriting"]

# 4 distinct ground-truth values per field type. Each gets rendered at all
# 5 degradation levels, giving 5 x 4 x 5 = 100 cases.
VALUES_BY_FIELD_TYPE: dict[str, list[str]] = {
    "typeset": [
        "Acute myocardial infarction",
        "Cardiac arrest secondary to sepsis",
        "Multi-organ failure",
        "Municipal Corporation of Greater Mumbai",
    ],
    "name": [
        "Ramesh Kumar Sharma",
        "Priya Anand Nair",
        "Fatima Bano Sheikh",
        "Arjun Vinod Deshmukh",
    ],
    "numeric": ["4471829", "88213", "150000", "2026004417"],
    "date": ["12-03-2026", "04-01-2026", "17-07-2025", "29-11-2026"],
    "handwriting": [
        "Septic shock",
        "Respiratory failure",
        "Sudden cardiac death",
        "Renal failure",
    ],
}


def new_card() -> Image.Image:
    return Image.new("RGB", CANVAS_SIZE, PAPER)


def degrade_whole(img: Image.Image, rotation_deg: float, noise_sigma: float, blur_radius: float, jpeg_quality: int, vignette: bool) -> Image.Image:
    img = img.rotate(rotation_deg, expand=False, fillcolor=PAPER, resample=Image.BICUBIC)
    arr = np.asarray(img).astype(np.float32)
    if vignette:
        h, w = arr.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = w / 2, h / 2
        dist = np.sqrt(((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2)
        vig = np.clip(1.0 - 0.28 * np.clip(dist - 0.55, 0, None), 0.7, 1.0)
        arr *= vig[..., None]
    noise = np.random.default_rng(42).normal(0, noise_sigma, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def degrade_region(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    x0, y0, x1, y1 = (int(box[0]) - 6, int(box[1]) - 6, int(box[2]) + 6, int(box[3]) + 6)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    region = img.crop((x0, y0, x1, y1))
    w, h = region.size
    if w < 1 or h < 1:
        return img
    small = region.resize((max(1, w // 10), max(1, h // 10)), Image.BILINEAR)
    region = small.resize((w, h), Image.NEAREST)
    region = region.filter(ImageFilter.GaussianBlur(radius=6.0))
    arr = np.asarray(region).astype(np.float32)
    noise = np.random.default_rng(7).normal(0, 55.0, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    region = Image.fromarray(arr)
    img.paste(region, (x0, y0))
    return img


def render_case(field_type: str, value: str, degradation: str) -> Image.Image:
    img = new_card()
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, CANVAS_SIZE[0] - 10, CANVAS_SIZE[1] - 10], outline=INK, width=2)

    label = "Field:"
    draw.text((40, 80), label, font=label_font, fill=INK)
    label_bbox = draw.textbbox((40, 80), label + "   ", font=label_font)
    value_x = label_bbox[2]

    if degradation == "absent":
        return img

    font = hand_font if field_type == "handwriting" else value_font
    color = HAND_INK if field_type == "handwriting" else INK
    value_y = 74 if field_type == "handwriting" else 78
    draw.text((value_x, value_y), value, font=font, fill=color)
    value_bbox = draw.textbbox((value_x, value_y), value, font=font)

    if degradation == "clean":
        pass
    elif degradation == "mild":
        img = degrade_whole(img, rotation_deg=1.0, noise_sigma=10.0, blur_radius=0.3, jpeg_quality=65, vignette=False)
    elif degradation == "heavy":
        img = degrade_whole(img, rotation_deg=2.4, noise_sigma=22.0, blur_radius=0.7, jpeg_quality=22, vignette=True)
    elif degradation == "illegible":
        img = degrade_region(img, value_bbox)
    else:
        raise ValueError(f"unknown degradation level: {degradation}")

    return img


def build_cases() -> list[dict]:
    cases = []
    for field_type in FIELD_TYPES:
        for value_idx, value in enumerate(VALUES_BY_FIELD_TYPE[field_type]):
            for degradation in DEGRADATION_LEVELS:
                case_id = f"{field_type}_{value_idx}_{degradation}"
                img = render_case(field_type, value, degradation)
                image_path = CASES_DIR / f"{case_id}.png"
                img.save(image_path)
                cases.append(
                    {
                        "case_id": case_id,
                        "field_type": field_type,
                        "value_index": value_idx,
                        "degradation_level": degradation,
                        "true_value": None if degradation == "absent" else value,
                        "image_path": image_path.relative_to(OUT_DIR).as_posix(),
                    }
                )
    return cases


# Each (field_type, degradation_level) cell has exactly 4 cases (one per
# ground-truth value). To hit an exact global 40/30/30 split while keeping
# every cell represented in every split (not just every split represented
# in aggregate), each cell's 4 cases are assigned via one of three fixed
# patterns. Solving 2x+y+z=40, x+2y+z=30, x+y+z=25 (25 cells total) gives
# x=15, y=5, z=5 -- i.e. 15 cells use PATTERN_A, 5 use PATTERN_B, 5 use
# PATTERN_C, which sums to exactly tune=40, validation=30, test=30.
PATTERN_A = ["tune", "tune", "validation", "test"]  # majority pattern (15/25 cells)
PATTERN_B = ["tune", "validation", "validation", "test"]  # 5/25 cells
PATTERN_C = ["tune", "validation", "test", "test"]  # 5/25 cells


def split_cases(cases: list[dict]) -> list[dict]:
    """Assigns each case to tune (40%) / validation (30%) / test (30%),
    stratified so every (field_type, degradation_level) cell contributes to
    all three splits -- not just balanced in aggregate. Which pattern a
    cell uses is rotated by (level_index - type_index) mod 5, so no single
    field type or degradation level is systematically stuck with the same
    pattern (which would silently bias one split toward a specific type or
    level)."""
    for case in cases:
        type_idx = FIELD_TYPES.index(case["field_type"])
        level_idx = DEGRADATION_LEVELS.index(case["degradation_level"])
        offset = (level_idx - type_idx) % 5
        pattern = PATTERN_B if offset == 0 else PATTERN_C if offset == 1 else PATTERN_A
        case["split"] = pattern[case["value_index"]]
    return cases


def print_distribution(cases: list[dict]) -> None:
    from collections import Counter

    for split in ["tune", "validation", "test"]:
        subset = [c for c in cases if c["split"] == split]
        by_type = Counter(c["field_type"] for c in subset)
        by_level = Counter(c["degradation_level"] for c in subset)
        print(f"{split}: {len(subset)} cases -- by field_type={dict(by_type)} by degradation_level={dict(by_level)}")


if __name__ == "__main__":
    CASES_DIR.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    cases = split_cases(cases)
    MANIFEST_PATH.write_text(json.dumps(cases, indent=2), encoding="utf-8")
    print(f"Generated {len(cases)} cases in {CASES_DIR}, manifest at {MANIFEST_PATH}")
    print_distribution(cases)
