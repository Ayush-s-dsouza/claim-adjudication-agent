# Claim Adjudication Agent (demo)

A small demo built for a proposal to Sarvam AI: claims-intake tooling for
Indian life insurers that gets a death claim file **decision-ready** for a
human adjudicator, faster.

## What this is

Point it at a folder of claim documents. It extracts the fields a death
claim needs, scores each field's confidence, flags anything missing or
inconsistent, and hands a human adjudicator a clean summary plus a
replayable audit trail. The pay/deny call is never made here.

## What this is not

- **Not an auto-adjudicator.** It never approves or denies a claim. Its only
  output is "here's what's ready, here's what a human needs to chase."
- **Not using real claims data.** Every document under `samples/` is
  synthetic -- fabricated for this repo. See [Synthetic data](#synthetic-data).
- **Not production-hardened.** No auth, no PII handling policy, no retry/
  backoff tuning beyond the basics. It's a demo of the architecture, not a
  deployable service.

## Key finding: confidence 1.0 does not mean correct

`claim_002`'s death certificate has one field, `place_of_death`, deliberately
made illegible -- pixelated, blurred, and noised until a human can't read it
(open the image; the rest of the page is untouched). The real value rendered
there, before degradation, was **"Mumbai"**. This was a direct test of
whether `sufficiency.py`'s confidence threshold (`CONFIDENCE_THRESHOLD =
0.80`) would actually catch a bad extraction from Sarvam's Extract API.

Four independent live calls on that exact same image returned:

| Call | Returned value | Confidence |
|---|---|---|
| 1 | `"LIG Colony, Mumbai"` -- fabricated; this text never existed on the document | 1.0 |
| 2 | `"Mumbai"` -- correct | 1.0 |
| 3 | *not found* | 0.0 |
| 4 | `"Mumbai"` -- correct | 1.0 |

Two things follow, and both matter more than a clean pass/fail would have:

1. **Confidence 1.0 does not mean correct.** Call 1 fabricated a more
   specific, plausible-sounding wrong answer at the same maximum confidence
   as the two calls that got it right. A confidence-threshold check --
   `sufficiency.py`'s only defense against a bad field -- would have let
   that fabricated value straight through to a human adjudicator's
   decision-ready summary, completely unflagged, because nothing about its
   self-reported confidence looked different from a correct extraction.
2. **The model doesn't consistently abstain on ambiguous input.** Call 3's
   "not found" is the one outcome `sufficiency.py` *would* have caught.
   Whether the same input gets that safe outcome or a confidently wrong one
   looks close to a coin flip across these four calls.

This is direct, reproducible evidence for why the pay/deny call has to stay
human, and why sufficiency-checking can't be reduced to "trust the
confidence score." The structural checks in this repo that *don't* depend
on Sarvam accurately self-reporting its own uncertainty -- the missing-
document check and the cross-document name-mismatch check -- are the ones
that would have actually caught something wrong in this claim. The
confidence threshold, on its own, would not have.

## Architecture

```
schema.py        Pydantic models. No logic.
extract.py        <- the ONE model call (Sarvam Extract API)
sufficiency.py     <- deterministic rules, ZERO model calls
audit.py          Append-only JSONL log, replayable per claim.
run.py            CLI: wires the above together, makes no decisions itself.
samples/          Synthetic sample claims + the script that generated them.
```

The split that matters most: **extraction is a model call, sufficiency
checking is plain Python.** `sufficiency.py` imports nothing from Sarvam and
never calls a model -- it's a fixed list of required documents, required
fields, a confidence threshold, and a string-similarity check for
cross-document name mismatches (see the constants at the top of that file).
A regulator or auditor can read that file top to bottom and know exactly
what triggers a "needs human review" flag, without trusting a model's
reasoning. `extract.py` is the only file that talks to Sarvam, and its job
ends at producing `(value, confidence, source)` per field -- it makes no
sufficiency judgment of its own.

## What's verified vs. assumed

This matters for a repo going to Sarvam, so it's stated plainly.

**Verified against the real Sarvam API** (live smoke test, not just docs):
`run.py samples/claim_001` and `run.py samples/claim_002` were both run
end-to-end against a real `SARVAM_API_KEY`. The full path works as
designed: job submission, polling, `result` + `annotations` parsing,
per-field confidence, document + page source pointers, the sufficiency
check, and the audit log replay. `claim_001` came back decision-ready;
`claim_002` came back flagged for the missing KYC document and the name
mismatch, exactly as intended.

Worth being honest about from that live run:
- Most fields on both claims came back with confidence at or extremely
  close to `1.0`, including the handwriting-style field -- that's a real
  result, not fabricated, but it's a weak test of robustness: the
  "handwriting" is a clean, consistent script *font*
  (`samples/generate_samples.py`'s `hand_font`), not genuine irregular
  human handwriting, and the mild scan artifacts (skew, sensor noise,
  vignetting, JPEG recompression) didn't move confidence at all. This
  confirms the pipeline works, not that Extract is robust to real
  handwriting or severe degradation -- that would need real scanned
  documents to test properly.
- Confidence *did* eventually prove uninformative, but only once the input
  was made genuinely illegible rather than just noisy -- see
  [Key finding](#key-finding-confidence-10-does-not-mean-correct) above.
- The request/response shapes themselves were built against the installed
  `sarvamai` SDK's actual source (`sarvamai==0.1.31`, generated by Fern
  from Sarvam's real OpenAPI spec), which is how the contradictions in
  Sarvam's public docs (see below) got resolved before ever making a live
  call -- the live run then confirmed that source was right.

**Still not verified by this demo:** the documented file-size limit (50MB
vs 200MB disagreement) and free-credit amount (₹100 vs ₹1,000
disagreement) weren't stress-tested here -- the sample documents are small
single-page images, well under any plausible limit. Check your own
dashboard for the real numbers.

**Specific documented gaps, not hidden:**
- Extract's source pointer is **document + page only** -- there is no
  bounding box or region within the page. (Digitise's JSON output does
  include block-level bounding boxes, per the SDK docstring, but Extract
  does not.) Don't read `SourcePointer` as more precise than that.
- Handwriting support is explicitly marketed for **Digitise**
  ("handwritten forms and notes" is a named use case, insurance claims
  included) but is never mentioned for **Extract** specifically, and no
  accuracy numbers are published either way. `claim_002`'s cause-of-death
  field was extracted at ~1.0 confidence in the live run -- but see the
  caveat above, that field is a clean script font, not real handwriting,
  so this is not a genuine handwriting-robustness result either way.
- Sarvam's own docs disagree with themselves on file-size limits (50MB vs
  200MB across two pages) and free signup credit (₹100 vs ₹1,000 across two
  pages). Check your own dashboard rather than trusting either number here.
- The SDK is pre-1.0 (`0.1.31`) -- the interface may change under it.

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env          # then add your key from https://dashboard.sarvam.ai
python run.py samples/claim_001    # clean claim -> should come out decision-ready
python run.py samples/claim_002    # messy claim -> should come out flagged
```

Regenerate the synthetic samples at any time with:

```bash
python samples/generate_samples.py
```

Each run writes an append-only audit trail to `audit_logs/<claim_id>.jsonl`.

## Synthetic data

Everything under `samples/` is **fabricated for this demo**: fictional
people (e.g. "Ramesh Kumar Sharma", "Priya Anand Nair"), a fictional
insurer ("Bharat Jeevan Life Insurance Co. Ltd."), fictional policy and
registration numbers. None of it is real claims data, and none of it
resembles any real person or filing. The generator is
`samples/generate_samples.py` if you want to see exactly how each field
was made up or produce your own variations.

Two sample claims, deliberately different:

- **`claim_001`** -- clean. All four required documents present, the
  deceased's name matches everywhere, no handwriting, no scan noise.
  Expected result: decision-ready.
- **`claim_002`** -- messy, on purpose:
  - `nominee_kyc.png` is missing entirely.
  - The deceased's name is "Ramesh Kumar Sharma" on the death certificate
    and claim intimation form, but "Ramesh Sharma" (middle name dropped) on
    the hospital discharge summary -- a realistic transcription mismatch.
  - The discharge summary's cause-of-death field is rendered in a
    handwriting-style font, not typeset text.
  - The death certificate has visible scan artifacts: skew, sensor noise,
    vignetting, and JPEG recompression, simulating a phone photo of a paper
    certificate.
  - The death certificate's `place_of_death` field is additionally smudged
    (heavy localized pixelation/blur/noise, genuinely unreadable) -- this is
    the field behind the [Key finding](#key-finding-confidence-10-does-not-mean-correct)
    above. Sarvam's returned confidence for it does *not* reliably signal
    whether the value is trustworthy, so `run.py`'s summary won't
    necessarily flag it -- that's the point being demonstrated, not a bug.
  - Expected result: **not** decision-ready, because of the missing document
    and the name mismatch -- both flagged, never silently resolved. The
    smudged field may or may not additionally show up as low-confidence
    depending on what Sarvam returns on that particular run.
