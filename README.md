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
eval/             Separate investigation: what does Sarvam's confidence
                  score actually measure? See "Confidence diagnosis" below.
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

## Confidence diagnosis: what it actually measures, what fixed it, what didn't

The [Key finding](#key-finding-confidence-10-does-not-mean-correct) above
was too important to leave as a one-off anecdote from four API calls. This
section is the full, disciplined investigation that followed: a frozen
baseline, hypothesis testing gated against a pre-committed acceptance
threshold, and a held-out test unlocked as a single deliberate decision.
All of it lives under
`eval/` and is reproducible -- see [Reproducing this](#reproducing-this)
at the end of this section.

### Phase 0: what does the confidence score actually measure?

Checked before writing any diagnostic code, in the SDK source and Sarvam's
own docs, not assumed:

- **Undocumented everywhere that would normally define it.** The SDK types
  `annotations` as an untyped `Dict[str, Any]`. The only definition
  anywhere in Sarvam's own docs is *"a per-field score... showing how sure
  Sarvam is about the value"* -- a self-assessment framing, not a
  calibration claim, not a methodology.
- **A near-miss worth recording**: an early search surfaced a plausible
  citation claiming confidence is "for risk-aware prioritization, not
  ground-truth correctness." Traced before repeating it -- the source
  turned out to be an unrelated arXiv paper that doesn't mention Sarvam at
  all. Flagging the near-miss here rather than quietly dropping it,
  because almost repeating a fabricated-but-plausible citation is exactly
  the failure mode this whole diagnosis is about.
- **Structural findings that held up**: Digitise has a `content_type:
  printed | handwritten | mixed` hint parameter; Extract has no
  equivalent -- there's no way to even tell Extract a document might be
  handwritten. Only one negative state exists in the API ("not found" = a
  dash) -- no separate "present but illegible" status. No `required`/
  `nullable` keyword in the schema DSL.
- **Verdict**: confidence behaves like an uncalibrated, self-reported
  model signal -- consistent with a Vision-Language Model architecture,
  not classical OCR character-confidence -- not a correctness metric.

### Phase 1-2: eval harness and the first baseline

Built a 100-case stratified eval set (5 field types x 5 degradation levels
x 4 values, `eval/generate_cases.py`), split 40/30/30 tune/validation/test
with the test split locked in code (`eval/splits.py::load_split` raises
unless `allow_test=True` is passed explicitly, and logs every access).
Baseline result on this English-synthetic set, 5 repeats per case:

- **Fabrication rate: 17.9%** (confident wrong answer on illegible/absent
  input).
- **Every one of 350 calls** reported confidence in `[0.8, 1.0)` -- no
  spread at all. Within that single bucket, accuracy was 92.9%: confidence
  has zero power to separate the correct 93% from the wrong 7%, because
  everything lands in the same bucket regardless of correctness.
- A single smoke-test case returned `value: null` (abstained) at
  **confidence 0.997** -- confidence isn't just decoupled from
  correctness, it's decoupled from *abstention* too. Confirmed at scale:
  100% of 115 abstentions in the full baseline carried confidence >= 0.8.

### The pivot: synthetic handwriting isn't real handwriting

Everything above used a clean script *font* (Segoe Print) to simulate
handwriting -- not genuine human handwriting. Since the pitch to Sarvam is
specifically about residual risk on real Indic-script claims paperwork,
that's a real gap: every number above measures how Sarvam handles a font
renderer, not real handwriting. Investigated a pivot to real handwriting
before assuming it was even feasible -- see below.

### Real-handwriting arm: dataset, licensing, and what it actually tests

**Dataset**: [IIIT-HW-Dev](https://cvit.iiit.ac.in/research/projects/cvit-projects/indic-hw-data)
(~95K genuine handwritten Devanagari words, 135 writers), collected by
CVIT, IIIT Hyderabad (Gongidi & Jawahar, ICDAR 2021). Downloaded directly
from `cvit.iiit.ac.in`, **not** the third-party `c3rl/IIIT-INDIC-HW-WORDS-Hindi`
HuggingFace mirror.

**Licensing -- stated plainly, not glossed over.** No explicit license
exists for this dataset anywhere: not on CVIT's own project page, not on
the HuggingFace mirror. Independently corroborated, not just this
project's own reading: querying `hardik90/indic-dataset-license-matrix`
(a dedicated HuggingFace-wide licensing compliance dataset) directly shows
its own audit of this exact dataset as `risk_level: HIGH`,
`license_tag: (none)`, `guidance: "RESTRICTED -- treat as
all-rights-reserved."` A named alternative, CPAR-2012 (figshare), was
checked before defaulting to IIIT-HW-Dev: it has a genuinely clean CC BY
4.0 license (confirmed via figshare's own API), but its actual public
files are isolated digits/characters only -- the word-level pangrams its
paper describes don't appear to be downloadable anywhere. Proceeding with
IIIT-HW-Dev, with real mitigations, not just a disclaimer:
- **No dataset files are committed, nor is anything composited from
  them.** `eval/real_handwriting/.cache/` and `.../cases/` are gitignored.
  Only `manifest.json` (text labels + metadata) is committed. Regenerate
  locally with `python -m eval.generate_real_handwriting`, which
  downloads the ~1.8GB archive on first run.
- Findings are reported in aggregate (rates, counts, a handful of
  illustrative examples), not as a redistributed dataset.
- Attribution: Gongidi, S. and Jawahar, C.V., *"iiit-indic-hw-words: A
  Dataset for Indic Handwritten Text Recognition,"* ICDAR 2021.

**Curation -- manual, and disclosed as such.** Scanned real labels from
the dataset's own `train.txt` and hand-picked words that are actually
Indian names/places (they appear at their natural frequency in real Hindi
text, roughly 7-9% of a random sample) plus a few deliberately
claim-relevant generic words (`स्वर्गीय` = late/deceased, `धनराशि` = sum
of money, `मरीजों` = patients). Automated name-filtering wasn't built --
not worth the effort at this eval-set size. See
`eval/generate_real_handwriting.py`'s `CURATED_SAMPLES` for exactly which
(label, source image) pairs were used and why.

**Real, stated limitations, not hidden:**
- **Only 2 of 5 field types.** This corpus has no handwritten numeric or
  date samples at all. There is no honest way to cover those field types
  with real handwriting from this source -- the real-handwriting arm
  covers `name` and `typeset` only.
- **Compositing is a template + real ink, not a photographed genuine
  document.** Real strokes are extracted from their source image
  (background estimated, ink isolated by threshold, alpha-composited onto
  the same card layout the synthetic arms use) rather than pasted
  naively -- a naive paste was tested and looked obviously collaged. The
  result is real handwriting on a synthetic form, not a scan of an actual
  filled-out claim document. It tests "can Sarvam read this real ink,"
  not "can it read a real claim document."
- **The `illegible_natural` tier is small by necessity**: only 2 samples
  per field type, because genuinely bad handwriting has to be found by
  looking at real images, not manufactured on demand the way synthetic
  degradation can be.

### Three-arm Phase 2 baseline comparison

| Metric | English (font) | Devanagari (font) | Real handwriting |
|---|---|---|---|
| Fabrication rate (aggregate) | 17.9% | 21.6% | 30.0% |
| Extraction accuracy (legible input) | 100.0% | 66.2% | 79.4% |
| Abstention rate (illegible/processed) | 64.3% | 63.5% | 60.0% |
| **Abstention rate (naturally illegible)** | n/a | n/a | **0.0%** |

**The headline finding**: on genuinely bad real handwriting -- no
synthetic degradation, just actual messy human writing -- Sarvam abstained
**0 times out of 20** and fabricated a confident wrong answer **14 times
out of 20 (70%)**, more than double the aggregate rate. Every wrong answer
came back at ~0.9999-1.0 confidence. Two examples, verified by reading the
actual output, not just the aggregate number: the word "कुंदेरा" (Kundera)
was misread differently and wrongly across all 5 repeated calls
("बंदेरा", "वृंदेरा", "ब्रंदेरा"...), each at ~0.9999 confidence; "अल्पसंख्यकों"
(minorities) was confidently misread as "अल्पसर्वरूपको" in 4 of 5 calls.

This is the opposite pattern from the synthetically-pixelated tier (60%
abstention there). The likely explanation: pixelated/blurred noise reads
to the model as "this is corrupted," a cue to abstain -- but genuinely
messy cursive handwriting still *looks like* normal handwriting, so the
model guesses instead of recognizing it can't parse it. That's precisely
the failure mode a font-based synthetic arm cannot produce, and precisely
the one that matters most in a claims-intake system reading real
handwritten forms.

A second, independent finding sits underneath that one: even **clean,
perfectly-rendered synthetic Devanagari** (zero real handwriting involved)
already showed real degradation (66.2% vs. English's 100% legible-input
accuracy). Verified across 12 distinct examples before trusting it: a
specific, repeatable pattern of dropped or transposed *matras* (dependent
vowel signs) -- e.g. "विफलता" (failure) losing its ि to become "वफलता,"
independently, five separate times. Matra placement is a well-documented
hard problem in Devanagari OCR (matras attach before/after/above/below
their base consonant in position-dependent ways). Some of the real-arm gap
is therefore script-level, and the real-handwriting arm adds a distinct,
larger failure mode on top of it.

### Phase 3: what fixed it, what didn't

Bounded, validation-gated iteration against the frozen baseline --
`eval/experiments.py`'s `decide()` accepts a change only if fabrication
rate improves by >=2 percentage points with no more than a small
accuracy-legible regression; a 6-accepted/12-experiment hard stop is
enforced in code, not just documented. Per the approved scope: hypotheses
1 and 2 only, hypothesis 3 added only if both failed.

- **Hypothesis 2 (self-consistency, k=3 of 5 repeats must agree or
  abstain) -- ACCEPTED.** Computed for free by re-aggregating the
  baseline's already-collected repeats, no new API calls. Fabrication
  rate 31.7% -> 25.0% (validation split), zero accuracy cost.
- **Hypothesis 1 (nullable schema + explicit "return null if illegible"
  instruction) -- REJECTED**, and the rejection itself is informative.
  It improved fabrication rate by a large 28.3 points, but cost 4.0 points
  of legible-input accuracy -- over the pre-committed 2-point tolerance.
  A different risk tolerance might take that trade; the point of
  pre-committing the threshold before running the experiment was to not
  let hindsight rationalize an exception in the moment. The gate rejected
  something with a real, non-trivial cost, which is what it's for.
- Hypothesis 2 succeeding meant **Hypothesis 3 (verification pass) was
  skipped**, per the plan's own stopping rule.

Final accepted configuration going into Phase 4: baseline extraction +
self-consistency (k=3 of 5) aggregation. The schema-instruction change is
not applied.

### Phase 4: held-out test, opened once

`eval/splits.py::load_split('test', allow_test=True, arm='real_handwriting')`
-- unlocked as a single deliberate decision, made once. The loader itself
was invoked 3 times that afternoon (all logged, timestamps in
`eval/real_handwriting/TEST_SET_ACCESS_LOG.jsonl`): once to confirm the
lock actually opened, then twice more while resuming an interrupted
125-call collection run. Each invocation just re-reads the same fixed
25-case list -- no new information leaks per call, and none of it changed
what got tested or how -- but "opened exactly once" would overstate the
literal call count, so the log is committed as-is rather than summarized
away.

| Metric | Validation (raw) | Validation (self-consistency) | Test (raw) | **Test (self-consistency)** |
|---|---|---|---|---|
| Fabrication rate | 31.7% | 25.0% | 27.5% | **14.3%** (1/7) |
| Accuracy (legible) | 73.3% | 73.3% | 72.9% | **70.6%** |

**No overfitting**: test tracks validation closely at both the raw and
self-consistency-applied level. Not claiming test came out *better* --
the 14.3% figure has a denominator of 7 cases (self-consistency collapses
5 repeats into 1 verdict per case, and the test split simply doesn't have
many illegible/absent cases), so a single flip moves it by ~14 points.
The honest read is "consistent with validation, within noise."

**One gap held-out testing cannot close, stated directly**: the test
split has **zero `illegible_natural` cases** -- all 4 of that tier's
samples (2 per field type; see the field-type limitation above) landed in
tune/validation during stratification, a direct consequence of the pool
being that small. The 70% naturally-illegible fabrication rate -- the
single most important number in this whole diagnosis -- is therefore
**not re-confirmed by held-out data**. It stands on the original n=20
tune+validation sample. Said plainly here rather than let the clean Phase
4 table imply everything was re-validated.

### Reproducing this

```bash
python -m eval.generate_cases                    # English-synthetic arm (committed already)
python -m eval.generate_cases_devanagari          # Devanagari-synthetic control arm
python -m eval.generate_real_handwriting          # downloads ~1.8GB from CVIT on first run

python -m eval.collect --experiment baseline --split tune validation --repeats 5
python -m eval.collect --experiment baseline --split tune validation --repeats 5 --arm synth_devanagari --language hi-IN
python -m eval.collect --experiment baseline --split tune validation --repeats 5 --arm real_handwriting --language hi-IN

python -m eval.metrics --experiment baseline [--arm <arm>]
python -m eval.experiments --arm real_handwriting --language hi-IN run --name h1... --hypothesis "..." --extra-instruction "..."
python -m eval.experiments --arm real_handwriting self-consistency --k 3 --n 5
python -m eval.experiments --arm real_handwriting status
```

Passing `--arm synth_devanagari` or `--arm real_handwriting` to `collect.py`
or `experiments.py` without an explicit `--language` is a hard error by
design -- an earlier run once defaulted to `en-IN` for Devanagari content
and silently produced meaningless results (see the git history on
`eval/collect.py` if you want the specifics of what that looked like and
how it was caught).

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
