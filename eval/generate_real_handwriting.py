"""Generates the real-handwriting arm: genuine human handwriting (not a
font simulation) composited onto the same "Field: <value>" template used
by the synthetic arms, evaluated with the identical protocol.

Source: IIIT-HW-Dev (Devanagari handwritten words), ~95K word images from
135 writers, collected by CVIT, IIIT Hyderabad (Gongidi & Jawahar).
Downloaded directly from cvit.iiit.ac.in -- NOT the third-party
`c3rl/IIIT-INDIC-HW-WORDS-Hindi` HuggingFace mirror. See the README's
"Real-handwriting arm" section for why: no explicit license exists for
this dataset anywhere (confirmed independently via a HF-wide licensing
compliance dataset), so this project treats it as all-rights-reserved and
uses it under research/citation norms only -- no dataset files, nor
anything composited from them, are ever committed (see .gitignore).
Regenerate locally with `python -m eval.generate_real_handwriting`, which
downloads the ~1.8GB archive on first run if the curated source crops
aren't already cached.

Curation (manual, not automated -- see the pivot plan for why an
automated Hindi name/place classifier wasn't worth building for this
size of eval set): scanned a few hundred labels from the dataset's own
train.txt and hand-picked real Indian names/places that appear in it
naturally (Hindi vocabulary corpora are built from real text, so proper
nouns show up at their natural frequency) plus a few claim-relevant
generic words. Every (label, source path) pair below was chosen by
actually reading the label file and, for "naturally illegible" entries,
by visually inspecting the rendered image -- not guessed.

Design differs from the synthetic arms in ways forced by the corpus, not
chosen for convenience:
- Only 2 of 5 field types: `name` and `typeset`. This corpus has no
  handwritten numeric or date samples at all -- there is no honest way to
  cover those field types with real handwriting from this source.
- 6 degradation levels, not 5: `illegible` splits into two tiers that are
  NOT the same claim about the model --
    - `illegible_processed`: a legible source crop, post-processed
      (same technique as the synthetic arms' illegible tier) -- directly
      comparable to synthetic.
    - `illegible_natural`: a source crop that is illegible because the
      handwriting itself is genuinely bad, zero post-processing applied --
      a failure mode the synthetic arms cannot produce (a font renderer
      can't write badly). Only 1-2 samples exist per field type because
      genuinely bad handwriting has to be found, not manufactured -- a
      real, honestly-reported sample-size limitation, not an oversight.
"""

from __future__ import annotations

import io
import json
import tarfile
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from eval.splits import assign_splits

OUT_DIR = Path(__file__).parent / "real_handwriting"
CACHE_DIR = OUT_DIR / ".cache"
ARCHIVE_PATH = CACHE_DIR / "IIIT-HW-Hindi_v1.tar.gz"
EXTRACTED_DIR = CACHE_DIR / "extracted"
IMAGES_ARCHIVE = EXTRACTED_DIR / "HindiSeg.tar.gz"
SELECTED_DIR = EXTRACTED_DIR / "selected"

CVIT_URL = "https://cvit.iiit.ac.in/images/Projects/wordlevel-Indicscripts/IIIT-HW-Hindi_v1.tar.gz"

CASES_DIR = OUT_DIR / "cases"
MANIFEST_PATH = OUT_DIR / "manifest.json"

CANVAS_SIZE = (1000, 220)
PAPER = (250, 248, 240)
INK = (15, 15, 15)

FONTS_TTC = r"C:\Windows\Fonts\Nirmala.ttc"
label_font = ImageFont.truetype(FONTS_TTC, 22, index=0)

# (label, path within HindiSeg.tar.gz). Chosen by reading actual labels in
# the dataset's train.txt, not synthesized -- see module docstring.
CURATED_SAMPLES: dict[str, dict[str, list[tuple[str, str]]]] = {
    "name": {
        "base": [
            ("शोभना", "HindiSeg/train/8/148/18.jpg"),
            ("जहाँगीर", "HindiSeg/train/4/165/7.jpg"),
            ("अखिलेश", "HindiSeg/train/2/130/8.jpg"),
            ("वसुंधरा", "HindiSeg/train/5/209/21.jpg"),
            ("व्यास", "HindiSeg/train/10/250/26.jpg"),
            ("जितेन्द्र", "HindiSeg/train/8/161/32.jpg"),
            ("भरत", "HindiSeg/train/8/252/17.jpg"),
            ("प्रमोद", "HindiSeg/train/5/183/15.jpg"),
        ],
        "naturally_illegible": [
            ("राहुल", "HindiSeg/train/10/234/23.jpg"),
            ("गौरी", "HindiSeg/train/7/133/13.jpg"),
        ],
    },
    "typeset": {
        "base": [
            ("स्वर्गीय", "HindiSeg/train/5/225/11.jpg"),
            ("धनराशि", "HindiSeg/train/8/172/2.jpg"),
            ("मरीजों", "HindiSeg/train/10/125/29.jpg"),
            ("राष्ट्रीय", "HindiSeg/train/8/52/2.jpg"),
            ("चयन", "HindiSeg/train/5/305/23.jpg"),
            ("व्यापारियों", "HindiSeg/train/4/149/11.jpg"),
            ("अनुशासन", "HindiSeg/train/10/185/19.jpg"),
            ("जनकपुर", "HindiSeg/train/8/32/13.jpg"),
        ],
        "naturally_illegible": [
            ("अल्पसंख्यकों", "HindiSeg/train/4/128/7.jpg"),
            ("कुंदेरा", "HindiSeg/train/1/185/26.jpg"),
        ],
    },
}


def ensure_source_images(paths: list[str]) -> None:
    """Downloads and extracts on demand whichever of `paths` aren't
    already cached locally. Never re-downloads what's already there."""
    missing = [p for p in paths if not (SELECTED_DIR / p).exists()]
    if not missing:
        return

    if not ARCHIVE_PATH.exists():
        print(f"Downloading {CVIT_URL} (~1.8GB, one-time)...")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(CVIT_URL, ARCHIVE_PATH)

    if not IMAGES_ARCHIVE.exists():
        print("Extracting outer archive...")
        with tarfile.open(ARCHIVE_PATH) as tf:
            tf.extract("HindiSeg.tar.gz", EXTRACTED_DIR, filter="data")

    print(f"Extracting {len(missing)} curated source image(s)...")
    SELECTED_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(IMAGES_ARCHIVE) as tf:
        for p in missing:
            tf.extract(p, SELECTED_DIR, filter="data")


def extract_ink_layer(source_path: Path, ink_color: tuple[int, int, int] = (30, 30, 110)) -> Image.Image:
    """Isolates handwriting strokes from their own paper background,
    returning a tightly-cropped RGBA image (fixed ink color, alpha =
    ink-ness). A naive paste of the whole source crop leaves an obviously
    collaged rectangle (mismatched background tone) -- verified directly
    during this pivot's investigation, not assumed. This is the fix."""
    img = Image.open(source_path).convert("RGB")
    arr = np.asarray(img).astype(np.float32)
    bg = np.median(arr.reshape(-1, 3), axis=0)
    dist = np.linalg.norm(arr - bg, axis=2)
    max_dist = max(float(dist.max()), 1e-6)
    # Higher gamma than a first attempt (2.2 vs 1.3) pushes faint
    # background/paper-grain pixels closer to fully transparent instead of
    # leaving a low-but-nonzero alpha halo around the strokes -- that halo
    # is what made an early version's "clean" tier show a faint rectangle.
    alpha = np.clip(dist / (max_dist * 0.55), 0, 1) ** 2.2
    alpha_img = Image.fromarray((alpha * 255).astype(np.uint8))
    # Feather the alpha edges (blur the mask, not the RGB) so the
    # transition to the destination background is a soft falloff rather
    # than a hard-edged cutout.
    alpha_img = alpha_img.filter(ImageFilter.GaussianBlur(radius=0.6))
    alpha_channel = np.asarray(alpha_img)

    rgba = np.zeros((*arr.shape[:2], 4), dtype=np.uint8)
    rgba[..., 0], rgba[..., 1], rgba[..., 2] = ink_color
    rgba[..., 3] = alpha_channel

    ys, xs = np.where(alpha_channel > 60)
    if len(xs) == 0:
        return Image.fromarray(rgba, mode="RGBA")
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    return Image.fromarray(rgba[y0:y1, x0:x1], mode="RGBA")


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


def render_case(source_path: Path, degradation: str) -> Image.Image:
    img = new_card()
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, CANVAS_SIZE[0] - 10, CANVAS_SIZE[1] - 10], outline=INK, width=2)

    label = "क्षेत्र:"
    draw.text((40, 80), label, font=label_font, fill=INK)
    label_bbox = draw.textbbox((40, 80), label + "   ", font=label_font)
    value_x = label_bbox[2]
    value_y = 70

    if degradation == "absent":
        return img

    ink_layer = extract_ink_layer(source_path)
    target_h = 55
    scale = target_h / ink_layer.height
    ink_resized = ink_layer.resize((max(1, int(ink_layer.width * scale)), target_h), Image.LANCZOS)

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(ink_resized, (value_x, value_y))
    img = img_rgba.convert("RGB")
    value_bbox = (value_x, value_y, value_x + ink_resized.width, value_y + ink_resized.height)

    if degradation in ("clean", "illegible_natural"):
        pass  # illegible_natural is illegible from the source handwriting itself -- no added processing
    elif degradation == "mild":
        img = degrade_whole(img, rotation_deg=1.0, noise_sigma=10.0, blur_radius=0.3, jpeg_quality=65, vignette=False)
    elif degradation == "heavy":
        img = degrade_whole(img, rotation_deg=2.4, noise_sigma=22.0, blur_radius=0.7, jpeg_quality=22, vignette=True)
    elif degradation == "illegible_processed":
        img = degrade_region(img, value_bbox)
    else:
        raise ValueError(f"unknown degradation level: {degradation}")

    return img


def build_cases() -> list[dict]:
    all_paths = [
        path
        for field_type in CURATED_SAMPLES.values()
        for pool in field_type.values()
        for _, path in pool
    ]
    ensure_source_images(all_paths)

    cases = []
    base_levels = ["clean", "mild", "heavy", "illegible_processed", "absent"]

    for field_type, pools in CURATED_SAMPLES.items():
        for value_idx, (label, rel_path) in enumerate(pools["base"]):
            source_path = SELECTED_DIR / rel_path
            for degradation in base_levels:
                case_id = f"{field_type}_{value_idx}_{degradation}"
                img = render_case(source_path, degradation)
                image_path = CASES_DIR / f"{case_id}.png"
                img.save(image_path)
                cases.append(
                    {
                        "case_id": case_id,
                        "field_type": field_type,
                        "value_index": value_idx,
                        "degradation_level": degradation,
                        "true_value": None if degradation == "absent" else label,
                        "image_path": image_path.relative_to(OUT_DIR).as_posix(),
                        "source_path": rel_path,
                    }
                )

        for value_idx, (label, rel_path) in enumerate(pools["naturally_illegible"]):
            source_path = SELECTED_DIR / rel_path
            case_id = f"{field_type}_natural{value_idx}_illegible_natural"
            img = render_case(source_path, "illegible_natural")
            image_path = CASES_DIR / f"{case_id}.png"
            img.save(image_path)
            cases.append(
                {
                    "case_id": case_id,
                    "field_type": field_type,
                    "value_index": value_idx,
                    "degradation_level": "illegible_natural",
                    "true_value": label,
                    "image_path": image_path.relative_to(OUT_DIR).as_posix(),
                    "source_path": rel_path,
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
