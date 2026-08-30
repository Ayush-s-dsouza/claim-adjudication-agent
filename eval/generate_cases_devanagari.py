"""Generates the Devanagari-synthetic CONTROL arm: the fair baseline the
real-handwriting arm is measured against.

Why this exists (see DECISION_LOG / the real-handwriting pivot plan): the
original English-synthetic arm (generate_cases.py) is entirely in English.
Comparing it directly against a Devanagari real-handwriting arm would
confound two variables at once -- real-vs-synthetic origin, AND
English-vs-Devanagari script. This arm holds script constant (Devanagari,
rendered via Windows' bundled Nirmala UI font) so the real-vs-synthetic
delta computed against it actually isolates what it claims to.

Same design as generate_cases.py: 5 field types x 4 values x 5 degradation
levels = 100 cases, same degrade_whole/degrade_region techniques. The one
real limitation, stated here rather than hidden: there is no Devanagari
equivalent of Segoe Print (the cursive font the English arm uses to
simulate handwriting) available on this system. The "handwriting" field
type here uses a different Nirmala weight (Semilight) for a little visual
distinction, but it is NOT a handwriting simulation the way the English
arm's is -- don't read its numbers as comparable to the English arm's
handwriting-field numbers.

This arm gets Phase 2 baseline only -- no Phase 3/4. It exists purely to
be the control the real-handwriting arm's Phase 2 baseline is compared
against (see eval/generate_real_handwriting.py).

    python eval/generate_cases_devanagari.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from eval.splits import assign_splits

OUT_DIR = Path(__file__).parent / "synth_devanagari"
CASES_DIR = OUT_DIR / "cases"
MANIFEST_PATH = OUT_DIR / "manifest.json"

CANVAS_SIZE = (1000, 220)  # slightly wider than the English arm: Devanagari conjuncts run wider per character
PAPER = (250, 248, 240)
INK = (15, 15, 15)
HAND_INK = (25, 35, 130)

FONTS_TTC = r"C:\Windows\Fonts\Nirmala.ttc"
label_font = ImageFont.truetype(FONTS_TTC, 22, index=0)  # Nirmala UI Regular
value_font = ImageFont.truetype(FONTS_TTC, 26, index=0)  # Nirmala UI Regular
hand_font = ImageFont.truetype(FONTS_TTC, 26, index=2)  # Nirmala UI Semilight -- NOT a handwriting simulation, see module docstring

DEGRADATION_LEVELS = ["clean", "mild", "heavy", "illegible", "absent"]
FIELD_TYPES = ["typeset", "name", "numeric", "date", "handwriting"]

# Same semantic content as generate_cases.py's VALUES_BY_FIELD_TYPE,
# translated/transliterated to Devanagari where the field type is
# script-bearing. Numeric and date stay in Arabic numerals -- that's the
# realistic convention on Indian institutional documents even when the
# surrounding text is Devanagari, so keeping them isolates the script
# variable to the fields where it actually matters (typeset/name/handwriting).
VALUES_BY_FIELD_TYPE: dict[str, list[str]] = {
    "typeset": [
        "तीव्र हृदयाघात",
        "पूति के कारण हृदयाघात",
        "बहु-अंग विफलता",
        "बृहन्मुंबई महानगरपालिका",
    ],
    "name": [
        "रमेश कुमार शर्मा",
        "प्रिया आनंद नायर",
        "फ़ातिमा बानो शेख",
        "अर्जुन विनोद देशमुख",
    ],
    "numeric": ["4471829", "88213", "150000", "2026004417"],
    "date": ["12-03-2026", "04-01-2026", "17-07-2025", "29-11-2026"],
    "handwriting": [
        "सेप्टिक शॉक",
        "श्वसन विफलता",
        "अचानक हृदय की मृत्यु",
        "गुर्दे की विफलता",
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

    label = "क्षेत्र:"  # "Field:"
    draw.text((40, 80), label, font=label_font, fill=INK)
    label_bbox = draw.textbbox((40, 80), label + "   ", font=label_font)
    value_x = label_bbox[2]

    if degradation == "absent":
        return img

    font = hand_font if field_type == "handwriting" else value_font
    color = HAND_INK if field_type == "handwriting" else INK
    value_y = 78
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
    cases = assign_splits(cases)
    MANIFEST_PATH.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Generated {len(cases)} cases in {CASES_DIR}, manifest at {MANIFEST_PATH}")
    print_distribution(cases)
