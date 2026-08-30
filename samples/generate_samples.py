"""Generates the synthetic sample claim documents under samples/.

Every document these functions produce is entirely fabricated for this
demo: fictional people, a fictional insurer, fictional policy and
registration numbers. None of it is real claims data. Re-run this script
to regenerate samples/claim_001 and samples/claim_002 from scratch.

    python samples/generate_samples.py
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).parent

CANVAS_SIZE = (1240, 1754)  # ~A4 at 150dpi
PAPER = (250, 248, 240)
INK = (15, 15, 15)
HAND_INK = (25, 35, 130)
SEAL_RED = (150, 20, 20)

FONTS_DIR = Path(r"C:\Windows\Fonts")
title_font = ImageFont.truetype(str(FONTS_DIR / "arialbd.ttf"), 32)
header_font = ImageFont.truetype(str(FONTS_DIR / "arialbd.ttf"), 22)
label_font = ImageFont.truetype(str(FONTS_DIR / "arial.ttf"), 20)
value_font = ImageFont.truetype(str(FONTS_DIR / "arial.ttf"), 20)
small_font = ImageFont.truetype(str(FONTS_DIR / "arial.ttf"), 14)
hand_font = ImageFont.truetype(str(FONTS_DIR / "segoepr.ttf"), 24)


def new_page() -> Image.Image:
    return Image.new("RGB", CANVAS_SIZE, PAPER)


def center_text(draw: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.FreeTypeFont, fill=INK) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    draw.text(((CANVAS_SIZE[0] - width) / 2, y), text, font=font, fill=fill)


def draw_field(
    draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str, handwritten: bool = False
) -> None:
    label_text = f"{label}:"
    draw.text((x, y), label_text, font=label_font, fill=INK)
    bbox = draw.textbbox((0, 0), label_text + "  ", font=label_font)
    value_x = x + (bbox[2] - bbox[0])
    if handwritten:
        draw.text((value_x, y - 3), value, font=hand_font, fill=HAND_INK)
    else:
        draw.text((value_x, y), value, font=value_font, fill=INK)


def apply_scan_artifacts(img: Image.Image, rotation_deg: float = 2.4, noise_sigma: float = 22.0) -> Image.Image:
    """Simulate a phone/scanner capture of a paper document: visible skew,
    sensor noise, a touch of blur, vignetting, and JPEG recompression."""
    img = img.rotate(rotation_deg, expand=False, fillcolor=PAPER, resample=Image.BICUBIC)
    arr = np.asarray(img).astype(np.float32)

    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy = w / 2, h / 2
    dist = np.sqrt(((xx - cx) / (w / 2)) ** 2 + ((yy - cy) / (h / 2)) ** 2)
    vignette = np.clip(1.0 - 0.28 * np.clip(dist - 0.55, 0, None), 0.7, 1.0)
    arr *= vignette[..., None]

    noise = np.random.default_rng(42).normal(0, noise_sigma, arr.shape)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.7))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=22)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def draw_seal(draw: ImageDraw.ImageDraw, cx: int, cy: int, text: str = "OFFICE\nSEAL") -> None:
    draw.ellipse([cx - 90, cy - 90, cx + 90, cy + 90], outline=SEAL_RED, width=3)
    draw.ellipse([cx - 78, cy - 78, cx + 78, cy + 78], outline=SEAL_RED, width=1)
    for i, line in enumerate(text.split("\n")):
        bbox = draw.textbbox((0, 0), line, font=small_font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w / 2, cy - 16 + i * 18), line, font=small_font, fill=SEAL_RED)


def death_certificate(
    path: Path,
    deceased_name: str,
    date_of_death: str,
    registration_number: str,
    place_of_death: str,
    issuing_authority: str,
    date_of_registration: str,
    scanned: bool = False,
) -> None:
    img = new_page()
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, CANVAS_SIZE[0] - 30, CANVAS_SIZE[1] - 30], outline=INK, width=3)
    draw.rectangle([38, 38, CANVAS_SIZE[0] - 38, CANVAS_SIZE[1] - 38], outline=INK, width=1)

    center_text(draw, 75, issuing_authority, title_font)
    center_text(draw, 118, "(Registration of Births & Deaths Act, 1969)", small_font)
    center_text(draw, 160, "DEATH CERTIFICATE", header_font)
    draw.line([100, 205, CANVAS_SIZE[0] - 100, 205], fill=INK, width=2)

    y = 260
    for label, value in [
        ("Registration No.", registration_number),
        ("Name of Deceased", deceased_name),
        ("Sex", "Male"),
        ("Date of Death", date_of_death),
        ("Place of Death", place_of_death),
        ("Date of Registration", date_of_registration),
    ]:
        draw_field(draw, 100, y, label, value)
        y += 65

    draw.line([100, y + 20, CANVAS_SIZE[0] - 100, y + 20], fill=INK, width=1)
    center_text(
        draw,
        y + 40,
        "This is to certify that the above information is true and extracted from the",
        small_font,
    )
    center_text(draw, y + 62, "register of deaths maintained by this office.", small_font)

    draw_seal(draw, CANVAS_SIZE[0] - 220, CANVAS_SIZE[1] - 220)
    draw.line([100, CANVAS_SIZE[1] - 170, 420, CANVAS_SIZE[1] - 170], fill=INK, width=1)
    draw.text((100, CANVAS_SIZE[1] - 160), "Registrar, Births & Deaths", font=label_font, fill=INK)

    if scanned:
        img = apply_scan_artifacts(img)
    img.save(path)


def hospital_discharge_summary(
    path: Path,
    hospital_name: str,
    deceased_name: str,
    date_of_admission: str,
    date_of_discharge: str,
    cause_of_death: str,
    handwritten_cause: bool = False,
) -> None:
    img = new_page()
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, CANVAS_SIZE[0] - 30, CANVAS_SIZE[1] - 30], outline=INK, width=2)

    center_text(draw, 70, hospital_name, title_font)
    center_text(draw, 112, "Department of General Medicine", small_font)
    center_text(draw, 150, "DISCHARGE SUMMARY", header_font)
    draw.line([100, 195, CANVAS_SIZE[0] - 100, 195], fill=INK, width=2)

    y = 250
    for label, value in [
        ("Patient Name", deceased_name),
        ("Age / Sex", "58 / Male"),
        ("UHID", "MH-UHID-88213"),
        ("Date of Admission", date_of_admission),
        ("Date of Discharge", date_of_discharge),
        ("Diagnosis", "Acute myocardial infarction"),
    ]:
        draw_field(draw, 100, y, label, value)
        y += 65

    y += 20
    draw.text((100, y), "Cause of Death:", font=label_font, fill=INK)
    if handwritten_cause:
        draw.text((100, y + 32), cause_of_death, font=hand_font, fill=HAND_INK)
    else:
        draw.text((100, y + 32), cause_of_death, font=value_font, fill=INK)

    draw.line([100, CANVAS_SIZE[1] - 170, 420, CANVAS_SIZE[1] - 170], fill=INK, width=1)
    draw.text((100, CANVAS_SIZE[1] - 160), "Attending Physician", font=label_font, fill=INK)
    img.save(path)


def claim_intimation_form(
    path: Path,
    insurer_name: str,
    policy_number: str,
    deceased_name: str,
    date_of_death: str,
    claimant_name: str,
    relationship_to_deceased: str,
) -> None:
    img = new_page()
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, CANVAS_SIZE[0] - 30, CANVAS_SIZE[1] - 30], outline=INK, width=2)

    center_text(draw, 70, insurer_name, title_font)
    center_text(draw, 150, "CLAIM INTIMATION FORM - DEATH CLAIM", header_font)
    draw.line([100, 195, CANVAS_SIZE[0] - 100, 195], fill=INK, width=2)

    y = 250
    for label, value in [
        ("Policy Number", policy_number),
        ("Name of Life Assured", deceased_name),
        ("Date of Death", date_of_death),
        ("Claimant Name", claimant_name),
        ("Relationship to Deceased", relationship_to_deceased),
        ("Contact Number", "+91 98765 43210"),
    ]:
        draw_field(draw, 100, y, label, value)
        y += 65

    y += 30
    center_text(
        draw,
        y,
        "I hereby declare that the information furnished above is true to the best of my knowledge.",
        small_font,
    )
    draw.line([100, CANVAS_SIZE[1] - 170, 420, CANVAS_SIZE[1] - 170], fill=INK, width=1)
    draw.text((100, CANVAS_SIZE[1] - 160), "Signature of Claimant", font=label_font, fill=INK)
    img.save(path)


def nominee_kyc(path: Path, claimant_name: str, id_type: str, id_number: str) -> None:
    img = new_page()
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 30, CANVAS_SIZE[0] - 30, CANVAS_SIZE[1] - 30], outline=INK, width=2)

    center_text(draw, 90, "NOMINEE KYC DOCUMENT", header_font)
    draw.line([100, 140, CANVAS_SIZE[0] - 100, 140], fill=INK, width=2)

    y = 220
    for label, value in [("Full Name", claimant_name), ("ID Type", id_type), ("ID Number", id_number)]:
        draw_field(draw, 100, y, label, value)
        y += 65
    img.save(path)


def build_claim_001() -> None:
    """Clean claim: all four documents present, consistent name, no
    handwriting, no scan noise. Should come out decision-ready."""
    claim_dir = OUT_DIR / "claim_001"
    claim_dir.mkdir(exist_ok=True)

    death_certificate(
        claim_dir / "death_certificate.png",
        deceased_name="Priya Anand Nair",
        date_of_death="04-01-2026",
        registration_number="MC/2026/00587",
        place_of_death="Kochi",
        issuing_authority="Kochi Municipal Corporation",
        date_of_registration="07-01-2026",
        scanned=False,
    )
    hospital_discharge_summary(
        claim_dir / "hospital_discharge_summary.png",
        hospital_name="Amrita Institute of Medical Sciences",
        deceased_name="Priya Anand Nair",
        date_of_admission="30-12-2025",
        date_of_discharge="04-01-2026 (deceased)",
        cause_of_death="Cardiac arrest secondary to acute myocardial infarction",
        handwritten_cause=False,
    )
    claim_intimation_form(
        claim_dir / "claim_intimation_form.png",
        insurer_name="Bharat Jeevan Life Insurance Co. Ltd.",
        policy_number="BJL-2291045",
        deceased_name="Priya Anand Nair",
        date_of_death="04-01-2026",
        claimant_name="Anand Nair",
        relationship_to_deceased="Spouse",
    )
    nominee_kyc(
        claim_dir / "nominee_kyc.png",
        claimant_name="Anand Nair",
        id_type="Aadhaar",
        id_number="XXXX XXXX 4471",
    )


def build_claim_002() -> None:
    """Messy claim: nominee KYC missing entirely, deceased's name spelled
    differently on the discharge summary, cause of death handwritten, and
    the death certificate has scan artifacts. Should come out NOT
    decision-ready with a specific chase list."""
    claim_dir = OUT_DIR / "claim_002"
    claim_dir.mkdir(exist_ok=True)

    death_certificate(
        claim_dir / "death_certificate.png",
        deceased_name="Ramesh Kumar Sharma",
        date_of_death="12-03-2026",
        registration_number="MC/2026/00214",
        place_of_death="Mumbai",
        issuing_authority="Municipal Corporation of Greater Mumbai",
        date_of_registration="15-03-2026",
        scanned=True,
    )
    hospital_discharge_summary(
        claim_dir / "hospital_discharge_summary.png",
        hospital_name="Shree Sai Hospital",
        deceased_name="Ramesh Sharma",  # middle name dropped -- deliberate mismatch
        date_of_admission="08-03-2026",
        date_of_discharge="12-03-2026 (deceased)",
        cause_of_death="Septic shock, multi-organ failure",
        handwritten_cause=True,
    )
    claim_intimation_form(
        claim_dir / "claim_intimation_form.png",
        insurer_name="Bharat Jeevan Life Insurance Co. Ltd.",
        policy_number="BJL-4471829",
        deceased_name="Ramesh Kumar Sharma",
        date_of_death="12-03-2026",
        claimant_name="Sunita Sharma",
        relationship_to_deceased="Spouse",
    )
    # nominee_kyc.png intentionally not generated for this claim.


if __name__ == "__main__":
    build_claim_001()
    build_claim_002()
    print(f"Generated samples in {OUT_DIR / 'claim_001'} and {OUT_DIR / 'claim_002'}")
