# Claim Adjudication Agent (demo)

This repo answers one question: how can claims-intake tooling for Indian 
life insurers actually be built on Sarvam's Extract API? There's a 
working pipeline here -- point it at a folder of claim documents and 
it extracts fields, checks sufficiency, and hands a human adjudicator 
a decision-ready summary. Testing that pipeline against genuinely 
handwritten Indic-script paperwork surfaced a specific, reproducible 
failure: on real bad handwriting, the model almost never admits it can't 
read something. It guesses, confidently, instead.

**Sarvam's Extract API is not documented as handling handwriting at
all.** Digitise (Sarvam's other document endpoint) explicitly supports a
`content_type: printed | handwritten | mixed` hint; Extract -- the
endpoint that actually returns structured fields with confidence scores,
the one any real intake pipeline would use -- has no equivalent. There's
no way to even tell Extract a document might be handwritten. So what
follows is what happens when handwritten claims paperwork, which is what
a real intake pipeline would actually receive, reaches an endpoint with
no handwriting affordance.

One more thing worth knowing before you read a single number: these are
results from a specific, deliberately adversarial evaluation on input
this endpoint isn't documented for, run at small sample sizes -- dozens
of cases per condition. That's the honest scope of every number below.

## What this is (and isn't)

Point it at a folder of claim documents and it extracts the fields a
death claim needs, scores each field's confidence, flags anything missing
or inconsistent, and hands a human adjudicator a clean, replayable
summary. The pay/deny call is never made here.

- **Not an auto-adjudicator.** Output is "here's what's ready, here's what
  a human needs to chase" -- never a decision.
- **Not using real claims data.** Everything under `samples/` is
  fabricated for this repo; everything under `eval/`'s real-handwriting
  arm comes from a public research dataset, used under the terms
  described in [the full diagnosis](#the-full-diagnosis) below. See also
  [Synthetic data](#synthetic-data).
- **Not production-hardened.** No auth, no PII handling policy, no
  retry/backoff tuning beyond the basics -- a demo of the architecture.

## The finding

On genuinely bad real handwriting -- actual messy human writing, zero
synthetic degradation applied -- Sarvam's Extract API abstained **0 times
out of 20 cases** and fabricated a confident wrong answer **14 times out
of 20 (70%, n=20)**, every wrong answer at ~0.9999-1.0 confidence. That's
more than double the fabrication rate seen on synthetically-degraded
input, where the model abstained 60% of the time instead of guessing.

Two examples, verified by reading the actual output: the word "कुंदेरा" (Kundera) was misread differently and
wrongly across all 5 repeated calls ("बंदेरा", "वृंदेरा", "ब्रंदेरा"...),
each at ~0.9999 confidence; "अल्पसंख्यकों" (minorities) was confidently
misread as "अल्पसर्वरूपको" in 4 of 5 calls (n=5 each).

**The likely mechanism**: pixelated or blurred noise reads to the model as
"this is corrupted" -- a cue to abstain. Genuinely messy handwriting
doesn't trigger that same signal; it still *looks like* normal
handwriting, so the model guesses instead of recognizing it can't parse
it. **Extract has no way to be told it
might be looking at handwriting in the first place.** A pipeline built to
read real claims paperwork will hit exactly this endpoint, with exactly
this gap.

**Where this finding came from.** The earliest version of this repo's
demo included one deliberately illegible field (`place_of_death` in a
synthetic sample claim) as a smoke test of whether the sufficiency
check's confidence threshold would catch a bad extraction. Four
independent live calls on that single field returned: a fabricated value
at confidence 1.0, two correct answers also at confidence 1.0, and one
correct abstention at confidence 0.0 (n=4) -- too small to be a finding on
its own, but consistent enough to be worth taking seriously. That n=4
oddity is what prompted the full three-arm investigation this README is
mostly about.

**What would close this gap:**
- A `content_type` hint on Extract, matching what Digitise already has,
  would let the model calibrate its behavior for handwritten input
  specifically instead of treating it identically to printed text.
- The API currently has exactly one negative state ("not found"). There's
  no "present but illegible" -- the model has no vocabulary to express "I
  can see something here but I'm not confident I read it right," distinct
  from "there's nothing here."

Both are affordances that don't exist in the API today. Their absence is
why this repo's sufficiency checking can't lean on Sarvam's own
confidence score, and has to be done with harness-level structural checks
instead -- which is what the rest of this README is about.

## What this means for building on Sarvam

Confidence cannot be used as a reliability signal, at any threshold: it
doesn't separate correct from incorrect (Phase 1-2, below), it doesn't
separate a found value from an abstention (also below), and on input this
kind of system actually needs to handle, it doesn't separate a guess from
a real answer. Any claims-intake system built on Extract has two
structural choices: keep a human in the loop for the actual pay/deny
call, and build independent checks that don't depend on Sarvam accurately
self-reporting its own uncertainty. This repo does both. Everything below
-- the architecture, then the full diagnosis that established the numbers
above -- follows from that finding: the diagnosis came first, the
architecture was built in response to it.

## Architecture

```
schema.py        Pydantic models. No logic.
extract.py        <- the ONE model call (Sarvam Extract API)
sufficiency.py     <- deterministic rules, ZERO model calls
audit.py          Append-only JSONL log, replayable per claim.
run.py            CLI: wires the above together, makes no decisions itself.
samples/          Synthetic sample claims + the script that generated them.
eval/             The full diagnosis behind the finding above -- see below.
```

The split that matters most: **extraction is a model call, sufficiency
checking is plain Python.** `sufficiency.py` imports nothing from Sarvam
and never calls a model -- it's a fixed list of required documents,
required fields, a confidence threshold, and a string-similarity check
for cross-document name mismatches (see the constants at the top of that
file). A regulator or auditor can read that file top to bottom and know
exactly what triggers a "needs human review" flag, without trusting a
model's reasoning. `extract.py` is the only file that talks to Sarvam,
and its job ends at producing `(value, confidence, source)` per field --
it makes no sufficiency judgment of its own.

This pipeline has been live-tested end-to-end against the real Sarvam
API: `run.py samples/claim_001` and
`run.py samples/claim_002` both run against a real `SARVAM_API_KEY`, and
the full path works as designed -- job submission, polling, `result` +
`annotations` parsing, per-field confidence, document + page source
pointers, the sufficiency check, and the audit log replay. See
[Limitations](#limitations) for what that live testing does *not* cover.

## The full diagnosis

Everything above is the headline. This is the disciplined investigation
behind it: a frozen baseline, hypothesis testing gated against a
pre-committed acceptance threshold, and a held-out test unlocked as a
single deliberate decision. All of it lives under `eval/` and is
reproducible -- see [Reproducing this](#reproducing-this) at the end of
this section.

### Phase 0: what does the confidence score actually measure?

Checked directly in the SDK source and Sarvam's own docs, before writing
any diagnostic code:

- **Undocumented everywhere that would normally define it.** The SDK
  types `annotations` as an untyped `Dict[str, Any]`. The only definition
  anywhere in Sarvam's own docs is *"a per-field score... showing how
  sure Sarvam is about the value"* -- a self-assessment framing, with no
  calibration claim and no stated methodology behind it.
- **A near-miss worth recording**: an early search surfaced a plausible
  citation claiming confidence is "for risk-aware prioritization, not
  ground-truth correctness." Traced before repeating it -- the source
  turned out to be an unrelated arXiv paper that doesn't mention Sarvam
  at all. It's flagged here because almost repeating a
  fabricated-but-plausible citation is exactly the failure mode this
  whole diagnosis is about.
- **The structural gap that matters most**: Digitise has a `content_type:
  printed | handwritten | mixed` hint parameter; Extract has no
  equivalent (see [The finding](#the-finding) above). Only one negative
  state exists in the API ("not found" = a dash) -- no separate "present
  but illegible" status. No `required`/`nullable` keyword in the schema
  DSL.
- **Verdict**: confidence behaves like an uncalibrated, self-reported
  model signal, consistent with a Vision-Language Model architecture
  rather than classical OCR character-confidence.

### Experimental design: two controls, one treatment arm

Three arms, designed as a controlled comparison from the outset:

- **English-synthetic** (`eval/generate_cases.py`) -- the original
  control. PIL-rendered text, a script font (Segoe Print) standing in for
  handwriting.
- **Devanagari-synthetic** (`eval/generate_cases_devanagari.py`) -- a
  second control, holding script constant. Needed because the English
  arm alone can't isolate whether a real-vs-synthetic gap comes from
  genuine handwriting or just from switching languages -- comparing
  English-synthetic directly against Devanagari real handwriting would
  confound origin and script in a single number. This arm exists
  specifically to prevent that.
- **Real handwriting** (`eval/generate_real_handwriting.py`) -- the
  treatment arm: genuine human handwriting from a real dataset,
  composited onto the same template the synthetic arms use.

The gap between the two synthetic controls and the treatment arm *is* the
finding -- that's the shape of a controlled experiment, and it's the
reason three arms exist instead of one.

Phase 3 (hypothesis testing, below) was scoped to the real arm only, by
design: that's the distribution the thesis is actually about, so that's
where the iteration budget went. The English arm's own Phase 3 run was
started once (`h1_nullable_instruction`, paused at 5 of 150 calls when
this investigation's scope shifted to building the three-arm comparison)
and intentionally not resumed -- finishing it would have answered a
question this repo isn't trying to answer with that arm.

### The real-handwriting arm: dataset, licensing, curation

**Dataset**: [IIIT-HW-Dev](https://cvit.iiit.ac.in/research/projects/cvit-projects/indic-hw-data)
(~95K genuine handwritten Devanagari words, 135 writers), collected by
CVIT, IIIT Hyderabad (Gongidi & Jawahar, ICDAR 2021). Downloaded directly
from CVIT's own project page (`cvit.iiit.ac.in`); the
`c3rl/IIIT-INDIC-HW-WORDS-Hindi` HuggingFace mirror is an unaffiliated
third-party repackaging and wasn't used as the source.

**Licensing -- stated plainly.** No explicit license exists for this
dataset anywhere: not on CVIT's own project page, not on the HuggingFace
mirror. Querying `hardik90/indic-dataset-license-matrix` (a dedicated
HuggingFace-wide licensing compliance dataset) directly confirms the same
audit result independently: `risk_level: HIGH`, `license_tag: (none)`,
`guidance: "RESTRICTED -- treat as all-rights-reserved."` A named
alternative, CPAR-2012 (figshare), was checked before defaulting to
IIIT-HW-Dev: it has a genuinely clean CC BY 4.0 license (confirmed via
figshare's own API), but its actual public files are isolated
digits/characters only -- the word-level pangrams its paper describes
don't appear to be downloadable anywhere. Proceeding with IIIT-HW-Dev,
with these mitigations:
- **No dataset files are committed, nor is anything composited from
  them.** `eval/real_handwriting/.cache/` and `.../cases/` are
  gitignored. Only `manifest.json` (text labels + metadata) is committed.
  Regenerate locally with `python -m eval.generate_real_handwriting`,
  which downloads the ~1.8GB archive on first run.
- Findings are reported in aggregate (rates, counts, a handful of
  illustrative examples), not as a redistributed dataset.
- Attribution: Gongidi, S. and Jawahar, C.V., *"iiit-indic-hw-words: A
  Dataset for Indic Handwritten Text Recognition,"* ICDAR 2021.

**Curation -- manual, and disclosed as such.** Scanned real labels from
the dataset's own `train.txt` and hand-picked words that are actually
Indian names/places (they appear at their natural frequency in real
Hindi text, roughly 7-9% of a random sample) plus a few deliberately
claim-relevant generic words (`स्वर्गीय` = late/deceased, `धनराशि` = sum
of money, `मरीजों` = patients). Automated name-filtering wasn't built;
manual curation was the proportionate amount of effort at this eval-set
size. See `eval/generate_real_handwriting.py`'s `CURATED_SAMPLES` for exactly which
(label, source image) pairs were used and why.

**Real, stated limitations:**
- **Only 2 of 5 field types.** This corpus has no handwritten numeric or
  date samples at all. There is no honest way to cover those field types
  with real handwriting from this source -- the real-handwriting arm
  covers `name` and `typeset` only.
- **Compositing places real ink on a synthetic template.** Strokes are
  extracted from their source image -- background estimated, ink isolated
  by threshold, alpha-composited onto the same card layout the synthetic
  arms use. (A naive paste was tried first and looked obviously collaged,
  which is why the extraction step exists.) This tests whether Sarvam can
  read real handwriting, not whether it can read a real filled-out claim
  document.
- **The `illegible_natural` tier is small by necessity**: only 2 samples
  per field type, because genuinely bad handwriting has to be found by
  looking at real images -- synthetic degradation can be manufactured on
  demand, this can't. This is also the tier the headline 70% figure comes
  from -- see the n=20 caveat under Phase 4.

### Three-arm baseline comparison

| Metric | English (font) | Devanagari (font) | Real handwriting |
|---|---|---|---|
| Fabrication rate (aggregate) | 17.9% (n=350) | 21.6% (n=349) | 30.0% (n=295) |
| Extraction accuracy (legible input) | 100.0% (n=210) | 66.2% (n=349) | 79.4% (n=295) |
| Abstention rate (illegible/processed) | 64.3% | 63.5% | 60.0% |
| **Abstention rate (naturally illegible)** | n/a | n/a | **0.0% (0/20)** |
| **Fabrication rate (naturally illegible)** | n/a | n/a | **70.0% (14/20)** |

The naturally-illegible row is [the finding](#the-finding) already stated
above, restated here in the full comparison for context: it's the
sharpest gap in the table, and the opposite pattern from the
synthetically-pixelated tier (60% abstention there).

A second, independent finding sits underneath that one: even **clean,
perfectly-rendered synthetic Devanagari** (zero real handwriting
involved) already showed real degradation (66.2% vs. English's 100%
legible-input accuracy). Verified across 12 distinct examples before
trusting it: a specific, repeatable pattern of dropped or transposed
*matras* (dependent vowel signs) -- e.g. "विफलता" (failure) losing its ि
to become "वफलता," independently, five separate times. This is
consistent with a **known, well-documented hard problem in Devanagari
OCR/HTR generally**: matras attach to their base consonant in
position-dependent ways -- before, after, above, or below it -- and
multiple independent studies identify
exactly this as a source of segmentation and recognition difficulty (e.g.
["Challenges in recognition of Devanagari Scripts due to segmentation of
handwritten text," IEEE](https://ieeexplore.ieee.org/document/7724755);
["Handwritten Devanagari Script Segmentation: A Non-linear Fuzzy
Approach"](https://arxiv.org/abs/1501.05472)). Some of the real-arm gap
is therefore script-level and expected from the literature; the
real-handwriting arm adds a distinct, larger failure mode on top of it.

### Phase 3: what fixed it, what didn't

Bounded, validation-gated iteration against the frozen baseline --
`eval/experiments.py`'s `decide()` accepts a change only if fabrication
rate improves by >=2 percentage points with no more than a small
accuracy-legible regression; a 6-accepted/12-experiment hard stop is
enforced in `check_budget()`, called before every experiment. Scoped to
the real arm only (see above). Per the approved plan: hypotheses 1 and 2
only, hypothesis 3 added only if both failed.

- **Hypothesis 2 (self-consistency, k=3 of 5 repeats must agree or
  abstain) -- ACCEPTED.** Computed for free by re-aggregating the
  baseline's already-collected repeats, no new API calls. Fabrication
  rate 31.7% -> 25.0% (validation split, n=30), zero accuracy cost.
- **Hypothesis 1 (nullable schema + explicit "return null if illegible"
  instruction) -- REJECTED.** This is arguably the single strongest
  methodological point in this repo: the change improved fabrication rate
  by a large 28.3 points, but cost 4.0 points of legible-input accuracy
  -- over the pre-committed 2-point tolerance. **A different risk
  tolerance would take that trade.** The point of pre-committing the
  threshold *before* running the experiment was specifically to prevent
  that call being made after seeing the numbers, when a 28-point
  improvement is sitting right there and easy to rationalize keeping. The
  gate rejected a change with a large, real benefit, on a rule agreed to
  in advance. That's what it's for.
- Hypothesis 2 succeeding meant **Hypothesis 3 (verification pass) was
  skipped**, per the plan's own stopping rule.

Final accepted configuration going into Phase 4: baseline extraction +
self-consistency (k=3 of 5) aggregation. The schema-instruction change is
not applied.

### Phase 4: held-out test, opened once

`eval/splits.py::load_split('test', allow_test=True, arm='real_handwriting')`
-- unlocked as a single deliberate decision, made once. The loader itself
was invoked 3 times that afternoon (all logged, timestamps in
`eval/real_handwriting/TEST_SET_ACCESS_LOG.jsonl`, committed to the
repo): once to confirm the lock actually opened, then twice more while
resuming an interrupted 125-call collection run. Each invocation re-reads
the same fixed 25-case list, so no new information leaked per call and
nothing about what got tested or how changed across the three calls. The
access log is committed as-is.

| Metric | Validation (raw) | Validation (self-consistency) | Test (raw) | **Test (self-consistency)** |
|---|---|---|---|---|
| Fabrication rate | 31.7% (n=30) | 25.0% (n=30) | 27.5% (n=25) | **14.3% (1/7)** |
| Accuracy (legible) | 73.3% (n=30) | 73.3% (n=30) | 72.9% (n=25) | **70.6% (n=24)** |

**No overfitting**: test tracks validation closely at both the raw and
self-consistency-applied level. The 14.3% test figure has a denominator
of 7 cases (self-consistency collapses 5 repeats into 1 verdict per case,
and the test split simply doesn't have many illegible/absent cases), so a
single flip moves it by ~14 points. Read it as "consistent with
validation, within noise" rather than as an improvement over validation's
25.0%.

**One gap held-out testing cannot close, stated directly**: the test
split has **zero `illegible_natural` cases** -- all 4 of that tier's
samples (2 per field type; see the field-type limitation above) landed
in tune/validation during stratification, a direct consequence of the
pool being that small. **The 70% naturally-illegible fabrication rate --
the single most important number in this whole diagnosis, and it rests
on n=20 -- is therefore not re-confirmed by held-out data.** It stands on
the original tune+validation sample alone. The Phase 4 table above does
not cover it.

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

Passing `--arm synth_devanagari` or `--arm real_handwriting` to
`collect.py` or `experiments.py` without an explicit `--language` is a
hard error by design -- an earlier run once defaulted to `en-IN` for
Devanagari content and silently produced meaningless results (see the git
history on `eval/collect.py` if you want the specifics of what that
looked like and how it was caught).

## Limitations

Everything in this section is a real, currently-true constraint on what
this repo has and hasn't shown. Each is also discussed in more depth in
context above; this section collects them in one place for reference.

- **The headline finding rests on n=20 and isn't re-confirmed by
  held-out data.** See [Phase 4](#phase-4-held-out-test-opened-once).
- **Extract has no handwriting affordance** (no `content_type` hint, no
  "present but illegible" state) -- this is the product gap the whole
  investigation is about. See [The finding](#the-finding).
- **The real-handwriting arm covers 2 of 5 field types** (`name` and
  `typeset` only) -- this corpus has no handwritten numeric or date
  samples.
- **Compositing is real ink on a synthetic template.** It tests reading
  real handwriting, not reading a filled-out claim form -- see
  [the real-handwriting arm section](#the-real-handwriting-arm-dataset-licensing-curation)
  for the full detail.
- **IIIT-HW-Dev has no explicit license**; used under research/citation
  norms with real mitigations (no committed dataset files or derivatives,
  aggregate reporting only) -- see
  [licensing](#the-real-handwriting-arm-dataset-licensing-curation).
- **Extract's source pointer is document + page only** -- there is no
  bounding box or region within the page. (Digitise's JSON output does
  include block-level bounding boxes, per the SDK docstring, but Extract
  does not.) Don't read `SourcePointer` as more precise than that.
- **The `sarvamai` SDK is pre-1.0** (`0.1.31`) -- the interface may
  change under it.
- **File-size limits and free-signup-credit amounts vary across Sarvam's
  own documentation pages** -- check your own dashboard for the current
  numbers. This repo doesn't stress-test either limit: every sample
  document used here is a small single-page image, well under any
  plausible limit.
- **The synthetic-arm "handwriting" field type (English and Devanagari)
  is rendered in a script font** (Segoe Print for English; no
  handwriting-styled Devanagari font exists on the system this was built
  on, so that arm's `handwriting` field type uses a different Nirmala
  weight instead). **It is not a handwriting simulation.** Any confidence
  numbers on that specific synthetic field type are a weak proxy at best
  -- the real-handwriting arm is what actually tests handwriting, and
  even that has the limitations listed above.

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

See [Reproducing this](#reproducing-this) above for running the full
confidence diagnosis.

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
    and claim intimation form, but "Ramesh Sharma" (middle name dropped)
    on the hospital discharge summary -- a realistic transcription
    mismatch.
  - The discharge summary's cause-of-death field is rendered in a
    handwriting-style font.
  - The death certificate has visible scan artifacts: skew, sensor
    noise, vignetting, and JPEG recompression, simulating a phone photo
    of a paper certificate.
  - The death certificate's `place_of_death` field is additionally
    smudged (heavy localized pixelation/blur/noise, genuinely
    unreadable) -- this is the field behind
    [where this finding came from](#the-finding) above. Sarvam's
    returned confidence for it does *not* reliably signal whether the
    value is trustworthy, so `run.py`'s summary won't necessarily flag
    it. That's the point being demonstrated.
  - Expected result: **not** decision-ready, because of the missing
    document and the name mismatch -- both flagged, never silently
    resolved. The smudged field may or may not additionally show up as
    low-confidence depending on what Sarvam returns on that particular
    run.
