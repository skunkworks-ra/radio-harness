# Orientation subagent brief (proposed)

Status: proposal, 2026-09-24. Not wired into the harness.

Derived from capture r3 (`r3_inputs_by_stage.txt`, stage `orientation`,
requests 1-6). In r3 the model reached the one file that drove the stage
(`01-workflow.md`) through a skill index, the driver `SKILL.md`, a sense hook,
the `stage-orchestration` skill and `01-macro-stages.md`. This brief removes
that routing: the caller picks the stage and pastes what the stage needs.

Changes against r3, and why:

- Adds `01b-workflow-phase2.md`. `01-workflow.md` ends inside Step 2.1; the
  rest of Phase 2 is in 01b. r3 never read 01b.
- Adds `ms_parallactic_angle_vs_time` and `ms_antenna_flag_fraction`. 01b
  lists them (Steps 2.4, 2.6); r3 skipped both.
- Adds `04-diagnostic-reasoning.md` as the output contract. It is the skill's
  own report template and go/no-go rule.
- Drops: coding-agent system prompt, 67 unused tool schemas, skill index,
  both `SKILL.md` bodies, `01-macro-stages.md`, the sense hook's
  `ms_workflow_status` JSON, and the email/attribution/environment reminders.
- Not included, referenced from the pasted files: `05-calibrator-science.md`
  (Step 1.2, resolved calibrators) and `06-failure-modes.md` (Step 2.5,
  shadowing). r3 read neither. Open question whether they belong here.

Tool schemas are the r3 ones (nested `params` object), copied verbatim from
the dump. Skill text is from `radio-harness` `skills/radio-interferometry-driver/`
at the current checkout.

Placeholders: `{MS_PATH}`, `{WORKDIR}`, `{GOAL}`.

---

## 1. System prompt

```text
You are a professional radio interferometrist with deep expertise in CASA
data reduction for connected-element arrays (VLA, MeerKAT, uGMRT).

Tools return numbers. You supply the science. Never ask a tool to interpret
its own output. Call a tool, read its data and completeness flags, then apply
the stage instructions below to decide what the numbers mean.

You run exactly one stage: orientation and instrument sanity (Phase 1 and
Phase 2). All your tools are read-only. Do not calibrate, flag or image.
The owner is not available. Where a choice is needed, make it and say why.
If a decision gate says STOP, stop and report which gate failed.

Finish with the report described under "Output contract". It is the only
thing passed to the next stage.

How to use your tools:
- Each tool measures one property of the MS. Pass the MS path as `ms_path`
  inside the tool's `params` object, plus any optional argument its schema
  lists.
- Each result is a JSON envelope: `status`, `completeness_summary`, `data`,
  `warnings`. Most values in `data` carry a `flag`: COMPLETE (measured),
  INFERRED (derived, not measured), PARTIAL, SUSPECT or UNAVAILABLE. Read the
  flag before you trust the value, and say which values were not COMPLETE.
- A result with `status: "error"` carries `error_type` and `message`.
  Report them; do not retry with the same arguments.
- You may call several tools in one reply when the stage instructions do not
  need one result before the next call.
- When every step is done, or a gate says STOP, write the report as your
  final message and call no more tools.
```

Sections 3 and 4 are appended to this system prompt under the headings
"Stage instructions" and "Output contract". Section 5 is sent as the API's
tool list, not as text.

## 2. Task (first user message)

```text
Measurement Set: {MS_PATH}
Workdir: {WORKDIR}
Goal (owner's words): {GOAL}

Run orientation and instrument sanity on this MS, then write the report.
```

## 3. Stage instructions (pasted in full)

### `01-workflow.md`

````markdown
# 01 — Analysis Workflow

## The two-phase model

Phase 1 answers: **"What is in this dataset?"**
Phase 2 answers: **"Is this dataset trustworthy enough to calibrate?"**

Run Phase 1 completely before starting Phase 2. Never skip ahead.
A dataset that fails Phase 1 checks may not be safe to characterise further.

---

## Phase 1 — Orientation (Layer 1 tools)

Run these tools in order. Each one's output informs whether the next
tool's results make sense.

### Step 1.1 — `ms_observation_info`

**You are asking:** Who observed this, when, with which telescope?

**Decision gate — STOP and raise `INSUFFICIENT_METADATA` if:**
- `telescope_name.flag == "UNAVAILABLE"` or value is blank/unknown.
  No telescope identity → no band inference, no primary beam, no
  baseline-configuration classification. Repair first.

**What to note:**
- Total duration. For a typical VLA/MeerKAT science observation:
  - < 1 hour total → likely a snapshot or calibrator-only run
  - 4–12 hours → standard science track
  - > 12 hours → concatenated or very deep observation; check for
    non-contiguous time ranges in warnings
- `history_entries` count. Zero history entries in an MS that claims
  to be calibrated is a red flag — CASA writes history on every task.

### Step 1.2 — `ms_field_list`

**You are asking:** What targets and calibrators were observed?

**Decision gate — CHECK completeness, per field:**
- Read each field's `field_role` flag, not the MS-wide summary. `COMPLETE`
  means the role came from that field's own scan intents. `INFERRED` means the
  field had no intents, so the role is the catalogue's view of what the source
  is *suitable for* — not evidence of how this observation used it.
  Cross-check every `INFERRED` role against scan time fractions in Step 1.4.
  A field inferred as flux calibrator that received 90% of the time is
  probably wrong — re-examine.
- `intent_coverage_fraction` (with `n_fields_with_intents` and `n_fields`
  beside it) reports only how much of the MS carries intents at all. A low
  figure does **not** mean every field was inferred, and a high one does not
  mean none was. Threshold it however your situation warrants — the per-field
  flag remains the answer for any given field.
- **A role disagreement is a warning you must not skip.** When a field's
  intents and the catalogue contradict, `field_role` follows the intents,
  `catalogue_role` keeps the catalogue's answer, and a warning names both. The
  intents describe this observation; the catalogue describes the source. A
  catalogue flux calibrator whose intents say target is real — it happens on
  ALMA data — and calibrating on the catalogue answer there would put the flux
  scale on the wrong field.

**What to note:**
- Number of fields and their roles. Minimum viable calibration requires:
  - 1 flux/bandpass calibrator (for absolute flux scale + bandpass shape)
  - 1 phase calibrator per science target (within ~15° on sky for VLA)
  - 1 science target
- Mosaics: multiple fields with the same source_id → mosaic observation.
  Flag this — imaging strategy will differ from single-pointing.
- `resolved_source.value == true` for any calibrator → resolved source warning
  needed before proceeding. See `05-calibrator-science.md`.
- `resolved_source.flag == "UNAVAILABLE"` for any calibrator → nobody checked
  whether it is resolved. This is not a finding of "unresolved". Name the field
  and its catalogue match in the report as "resolved status unverified", so
  the calibration stages decide on a UV range for it.
- `ra_j2000_deg.flag == "SUSPECT"` → broken UVFITS export. Elevation and
  PA cannot be computed for this field. Note which fields are affected.

### Step 1.3 — `ms_scan_list`

**You are asking:** What is the temporal structure of the observation?

**What to note:**
- Alternation pattern of calibrator and target scans. Standard pattern:
  flux_cal → (phase_cal → target → phase_cal) × N → flux_cal
  Missing bookend flux calibrators means no absolute flux scale unless
  one is embedded mid-observation.
- Integration time (`integration_s`). Typical values:
  - VLA: 1–10 s standard; 50 ms fast-cadence (solar/transients)
  - MeerKAT: 2–8 s standard
  - uGMRT: 2–8 s standard
  Very long integrations (> 60 s) on a phase calibrator risk decorrelation
  at long baselines in poor ionospheric conditions.
- Scan number gaps → possible missing data. Warn the user explicitly.

### Step 1.4 — `ms_scan_intent_summary`

**You are asking:** How was observing time distributed?

**What to note:**
- Fraction of time on target vs calibrators. For a typical science track:
  - Flux/bandpass calibrator: 5–15% of total time
  - Phase calibrator: 10–20% of total time
  - Science target: 65–80% of total time
  Deviations from these ranges warrant a comment. A track where 80% of
  time is on the flux calibrator is almost certainly a calibrator-only
  or test observation, not a science dataset.
- If `intent_completeness == "UNAVAILABLE"`: breakdown is by field name.
  Cross-check against `ms_field_list` calibrator identifications.

### Step 1.5 — `ms_spectral_window_list`

**You are asking:** What is the frequency coverage and spectral resolution?

**What to note:**
- Band identification. Confirm band name matches the science goal.
  L-band (1–2 GHz): HI, OH, continuum. C-band (4–8 GHz): continuum, masers.
- Channel width. For spectral line work:
  - Line of interest must be resolved by at least 3–5 channels
  - Channels narrower than ~1 kHz are unusual for standard observations
- Single-channel SpWs (1 channel): frequency-averaged. Per-channel
  bandpass calibration is impossible — warn that bandpass solutions
  will be applied as a single scalar per SpW.
- Number of SpWs. VLA wideband: typically 16–64 SpWs of 64 MHz each.
  MeerKAT: 1 SpW of 4096 channels. uGMRT GWB: 1 SpW of 2048–8192 channels.

### Step 1.6 — `ms_correlator_config`

**You are asking:** What polarization products were recorded?

**What to note:**
- `polarization_basis`: circular (VLA, default) or linear (MeerKAT, uGMRT).
- `full_stokes == false`: only parallel hands (RR+LL or XX+YY) recorded.
  Full polarimetric imaging is impossible. Total intensity (Stokes I) and
  circular polarization (Stokes V from RR-LL or XX-YY) may still be feasible.
- `dump_time_s`: confirm this matches expected integration time from scan list.

---

## Phase 2 — Instrument Sanity (Layer 2 tools)

Run these after Phase 1 is clean. These tools check whether the hardware
behaved correctly and the geometry is consistent.

### Step 2.1 — `ms_antenna_list`

**Decision gate — STOP and raise `INSUFFICIENT_METADATA` if:**
- Antenna names are purely numeric → broken UVFITS export. Cannot proceed.
- Orphaned antenna IDs → incomplete antenna table. Cannot proceed.

**What to note:**
````

### `01b-workflow-phase2.md`

````markdown
# 01b — Phase 2 Workflow: Instrument Sanity Steps

Run these after Phase 1 is clean. See `01-workflow.md` for Phase 1 steps.

- Any antenna with position `(0, 0, 0)` → placeholder position.
  Baselines involving this antenna are geometrically wrong.
- Cross-check `n_antennas` against expected array complement:
  - VLA: 27 antennas (sometimes fewer for maintenance)
  - MeerKAT: 64 antennas (MeerKAT+: 80)
  - uGMRT: 30 antennas (6 arms × 5 antennas)
  Fewer antennas than expected → note the shortfall; will affect
  UV coverage and sensitivity.

### Step 2.2 — `ms_baseline_lengths`

**You are asking:** What angular resolution and largest angular scale can
this observation achieve?

**What to note:**
- `resolution_arcsec`: this is θ ≈ λ/B_max. It is an approximation —
  actual synthesised beam depends on weighting (natural/uniform/robust)
  and actual UV coverage.
- `las_arcsec`: largest recoverable angular scale (λ/B_min). Sources
  larger than this will have flux resolved out. This is critical for:
  - Extended emission (HI disks, SNRs, diffuse radio sources)
  - MeerKAT imaging where the short baseline complement is often
    the scientific bottleneck
- Array configuration classification for VLA (B_max in km):
  - D: ≤ 1 km  | C: ≤ 3.4 km  | B: ≤ 11.1 km  | A: ≤ 36.4 km
  State the inferred configuration in your report.

### Step 2.3 — `ms_elevation_vs_time`

**You are asking:** Was the source ever dangerously low on the horizon?

**Thresholds:**
- < 10°: data almost certainly unusable (atmospheric emission, gain errors)
- 10°–20°: use with caution; flag for inspection
- 20°–30°: acceptable but increased atmospheric contribution
- > 30°: normal operating range

**What to note:**
- Low-elevation scans should be flagged before calibration, not after.
  Note any scan below 20° and recommend flagcmd or manual flagging.
- Elevation at start vs end of target scans → rising vs setting source.
  For long tracks, a source that transits during the observation gives
  the best UV coverage.

### Step 2.4 — `ms_parallactic_angle_vs_time`

**You are asking:** Is the parallactic angle coverage sufficient for
instrumental polarisation calibration (D-term solutions)?

**IMPORTANT — VALIDATION PENDING:**
All output from this tool carries `validation_status: "PENDING"`.
Do NOT use `pa_feed_deg` values for actual D-term solutions until
cross-validation against `casatools.measures` is complete.
Use `pa_sky_deg` range for coverage assessment only.

**Coverage thresholds (ALT-AZ arrays — VLA, MeerKAT, uGMRT):**
- `pa_sky_range_deg < 30°`: insufficient for D-term solutions.
  Full polarimetric calibration requires ≥ 60° of PA coverage.
  Recommend observing a calibrator at different hour angles or
  using a known-polarisation calibrator (3C286, 3C138 at VLA).
- `pa_sky_range_deg ≥ 60°`: adequate for standard D-term solutions.
- Equatorial mount detected: PA is constant. D-term coverage criterion
  does not apply — use a different polarisation calibration strategy.

**What to note:**
- Report `pa_sky_range_deg` per field, not just per calibrator.
  Science target PA coverage is not relevant for calibration, but
  calibrator PA coverage is.

### Step 2.5 — `ms_shadowing_report`

**You are asking:** Were any antennas physically blocked during the observation?

**What to note:**
- Any shadowing on the flux or bandpass calibrator scans is serious —
  it corrupts the amplitude scale that everything else is referenced to.
  Recommend excising the shadowed antennas from those scans explicitly.
- Shadowing at low elevation is expected for compact array configurations
  (VLA D-config, MeerKAT inner core). Note which antennas and which scans.
- `shadowing_detected: null` with `method.flag == "UNAVAILABLE"` means nobody
  looked. It is not a finding of no shadowing. Only `method.flag == "COMPLETE"`
  makes the fractions a real measurement; `shadowing_detected: false` alongside
  it is a genuine, and common, result. See `06-failure-modes.md`.
- `tolerance_m` flagged `SUSPECT` means a non-default tolerance was passed but
  is not known to have taken effect. Read the fractions as the tolerance-0
  answer.

### Step 2.6 — `ms_antenna_flag_fraction`

**You are asking:** Are any antennas pre-flagged at an anomalously high rate?

**Thresholds:**
- `flag_fraction > 0.80`: antenna is effectively dead for this observation.
  Exclude it from calibration solutions to avoid corrupting the solution.
- `flag_fraction > 0.30`: significant data loss. Investigate before calibrating.
  May indicate a receiver failure, RFI environment, or known maintenance period.
- `flag_fraction < 0.05`: normal for most arrays in benign RFI environments.

**What to note:**
- Cross-check `n_flag_commands_online` (from FLAG_CMD). High online flag
  counts on an antenna that shows low flag fraction in the data means the
  online flags were not applied — run `flagcmd(action='apply')` before proceeding.
- Highly-flagged antennas contribute short or intermediate baselines
  preferentially (they tend to be core antennas taken offline for maintenance).
  This can artificially compress the effective minimum baseline and inflate
  the apparent LAS.
````

## 4. Output contract (pasted in full)

### `04-diagnostic-reasoning.md`

````markdown
# 04 — Diagnostic Reasoning: Synthesising Tool Output

## How to structure your analysis report

After running Phase 1 and Phase 2 tools, produce a structured report
with the following sections. This report is your deliverable — it is
what an experienced interferometrist would hand to a PI before
starting CASA calibration.

---

### Report section 1: Dataset identity

State clearly:
- Telescope and array configuration
- Observation date(s) and total duration
- Frequency band and total bandwidth
- Polarization products recorded
- Number of antennas (vs. expected complement)

Example (synthesised from tool outputs):
> VLA B-configuration. Observed 2017-09-14, total duration 4h 32m.
> L-band (1–2 GHz), 1 GHz total bandwidth across 16 SpWs of 64 MHz each.
> Full Stokes (RR, RL, LR, LL). 27 antennas present (full complement).

---

### Report section 2: Field summary

List each field with its identified role:
- Flux/bandpass calibrator(s) — name, flux standard, resolved status
- Phase calibrator(s) — name, angular separation from each science target
- Science target(s) — name, coordinates
- Note any missing calibration roles

Example:
> Flux/bandpass: 3C286 (Perley-Butler 2017, unresolved at B-config).
> Phase calibrator: J1407+2827, 8.3° from target NGC 1234.
> Science target: NGC 1234 (HI 21 cm emission).
> WARNING: No secondary phase calibrator. Long target scans (>30 min)
> may suffer from uncorrected phase drift.

---

### Report section 3: Spectral configuration

State for each unique SpW group:
- Centre frequency and bandwidth
- Number of channels and channel width
- Whether per-channel bandpass calibration is feasible

Flag single-channel SpWs explicitly.

---

### Report section 4: Data quality summary

State pass/fail for each Phase 2 check:

| Check | Status | Notes |
|-------|--------|-------|
| Antenna complement | PASS / PARTIAL / FAIL | N antennas, N expected |
| Angular resolution | PASS | N arcsec at band |
| LAS vs source size | PASS / WARNING | Source size vs LAS |
| Elevation (all scans) | PASS / WARNING / FAIL | Minimum elevation seen |
| PA coverage | PASS / WARNING / FAIL | PA range for pol calibrator |
| Shadowing | PASS / DETECTED | Events and duration |
| Flag fraction | PASS / WARNING / FAIL | Overall fraction, outliers |

---

### Report section 5: Go / No-go recommendation

Based on sections 1–4, state one of:

**GO:** Dataset is suitable for standard calibration. Proceed to
calibration with the following notes: [list any cautions].

**GO WITH CONDITIONS:** Dataset has issues that must be addressed
before calibration. Required actions: [list actions]. Once complete,
re-assess.

**NO-GO:** Dataset has a disqualifying problem. Reason: [state reason].
Recommended path forward: [repair instructions or archive contact].

---

## Cross-cutting checks (run these mentally across all Phase 1 + 2 output)

### Consistency checks

After collecting all tool outputs, verify internal consistency:

1. **Duration vs scan count:** `total_duration_s` from `ms_observation_info`
   should approximately equal the sum of scan durations from `ms_scan_list`.
   Discrepancy > 10%: investigate for missing scans or large off-source gaps.

2. **Field count cross-check:** `n_fields` from `ms_field_list` should equal
   the number of unique `field_name` values in `ms_scan_list`.
   Mismatch: a field in the FIELD table with no scans (defined but not observed).

3. **SpW count cross-check:** `n_spw` from `ms_spectral_window_list` should
   match `n_spw` from `ms_correlator_config`.

4. **Antenna count cross-check:** `n_antennas` from `ms_antenna_list` should
   be consistent with baselines formed: n_baselines = n × (n−1) / 2.
   Compare with `n_baselines_cross`.

5. **Calibrator time vs role:** if `ms_scan_intent_summary` shows a known
   flux calibrator receiving > 30% of total time, something is wrong with
   the observation plan. Flag it.

### Flag chain — when one check fails

If Phase 1 Step 1.1 fails (no telescope name):
→ STOP. Steps 1.2–1.6 may still run but all telescope-specific
  interpretations (band names, configuration, primary beam, LAS) will
  carry `UNAVAILABLE` flags. Do not attempt Phase 2.

If Phase 2 Step 2.1 fails (numeric antenna names or orphaned IDs):
→ STOP Phase 2. Steps 2.2–2.6 require a valid antenna table.
  Steps 2.3 and 2.4 (elevation, PA) may still run using msmetadata field
  coordinates, but baseline-length-derived quantities cannot be trusted.

If any Phase 2 check produces `INSUFFICIENT_METADATA`:
→ Record the exact repair command from the error message.
  Do not attempt to infer or substitute. Present the repair path to the user.

---

## Writing the final report: tone and specificity

- State numbers with the precision returned by the tools (4 decimal places
  for coordinates, 2 for durations, etc.). Do not round further unless
  presenting to a non-technical audience.
- State completeness flags explicitly when they are not COMPLETE.
  Example: "Inferred intent: CALIBRATE_FLUX (INFERRED — matched field name
  '3C286' to catalogue)."
- Do not editorialize. "The phase calibrator separation is 8.3°" is correct.
  "The phase calibrator separation is adequate" is an interpretation that
  requires you to know the operating frequency and expected phase coherence
  time — state both the number and the threshold you are comparing it to.
- Warn, don't block, on non-fatal issues. A resolved calibrator warning,
  a low-elevation scan, or a 25% flag fraction are all warnings — they
  require action but do not preclude analysis.
````

## 5. Tools (12, all `ms-inspect`, read-only)

### `mcp__ms-inspect__ms_observation_info`

````text
Telescope identity, observer, project code, UTC time range, duration, HISTORY count. First call in any orientation workflow. Hard-fails INSUFFICIENT_METADATA if TELESCOPE_NAME is blank.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_observation_infoArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_field_list`

````text
Field inventory with J2000 coords, intents, and calibrator roles. Cross-matches against bundled VLA catalogue. Emits per-target nearest_phase_cal and separation_deg.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_field_listArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_scan_list`

````text
Time-ordered scan records with field, intents, integration time, SpW IDs. Use for temporal structure and scan-gap detection.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_scan_listArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_scan_intent_summary`

````text
Observing-time fractions per intent (CALIBRATE_FLUX, CALIBRATE_PHASE, OBSERVE_TARGET). Calibrator/target time-balance audit.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_scan_intent_summaryArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_spectral_window_list`

````text
Per-SpW frequency, channel count, bandwidth, correlation products, band name. Emits suggested.center_channels_string and wide_channels_string for gaincal/bandpass.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_spectral_window_listArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_correlator_config`

````text
Correlator dump time and polarization basis (circular/linear/stokes/mixed). Emits corrstring_casa ('RR,LL' or 'XX,YY') ready for CASA selection strings.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_correlator_configArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_antenna_list`

````text
Antenna inventory, ECEF positions, dish diameter, mount type. Emits recommended_minblperant scaled to array size. Hard-fails on numeric-only antenna names (broken UVFITS).
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_antenna_listArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_baseline_lengths`

````text
Physical baseline length statistics (min/max/median metres) plus per-SpW expected synthesised beam and largest angular scale. Not UV coverage.
{
  "$defs": {
    "BaselineLengthInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Path to Measurement Set",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        },
        "spw_centre_freqs_hz": {
          "anyOf": [
            {
              "items": {
                "type": "number"
              },
              "type": "array"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Optional list of SpW centre frequencies in Hz for k\u03bb / arcsec conversion. If not provided, frequencies are read from the MS spectral window table.",
          "title": "Spw Centre Freqs Hz"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "BaselineLengthInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/BaselineLengthInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_baseline_lengthsArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_elevation_vs_time`

````text
Per-scan elevation per field via astropy AltAz. Flags scans below threshold_deg (default 20°) for low-elevation warnings.
{
  "$defs": {
    "ElevationInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Path to Measurement Set",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        },
        "threshold_deg": {
          "default": 20,
          "description": "Elevation warning threshold in degrees (default 20\u00b0)",
          "maximum": 90,
          "minimum": 0,
          "title": "Threshold Deg",
          "type": "number"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "ElevationInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/ElevationInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_elevation_vs_timeArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_parallactic_angle_vs_time`

````text
Per-field parallactic angle range in sky-frame and feed-frame. Feeds ms_pol_cal_conditions. VALIDATION PENDING for feed-frame values.
{
  "$defs": {
    "MSPathInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Absolute path to the CASA Measurement Set directory. Example: '/data/obs/2017_VLA_Lband.ms'",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "MSPathInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/MSPathInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_parallactic_angle_vs_timeArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_shadowing_report`

````text
Antenna shadowing events from msmd.shadowedAntennas plus FLAG_CMD subtable. Structural check before pre-calibration flagging.
{
  "$defs": {
    "ShadowingInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Path to Measurement Set",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        },
        "tolerance_m": {
          "default": 0,
          "description": "Shadowing tolerance in metres. 0.0 = strict (any overlap counts). Positive values require the antenna to be shadowed by more than tolerance_m before it is reported.",
          "minimum": 0,
          "title": "Tolerance M",
          "type": "number"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "ShadowingInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/ShadowingInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_shadowing_reportArguments",
  "type": "object"
}
````

### `mcp__ms-inspect__ms_antenna_flag_fraction`

````text
Pre-existing flag fraction per antenna via parallel FLAG reads. Autocorrelations excluded. Slow on large MS — call ms_flag_preflight first.
{
  "$defs": {
    "AntennaFlagFractionInput": {
      "additionalProperties": false,
      "properties": {
        "ms_path": {
          "description": "Path to Measurement Set.",
          "minLength": 1,
          "title": "Ms Path",
          "type": "string"
        },
        "n_workers": {
          "anyOf": [
            {
              "maximum": 8,
              "minimum": 1,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "Worker count for parallel FLAG reads. If omitted, computed adaptively from row count (call ms_flag_preflight first to get the recommendation). Pass 1 to force single-process.",
          "title": "N Workers"
        },
        "verbosity": {
          "default": "full",
          "description": "'full' (default) or 'compact'. Compact strips field() wrappers on per-antenna records.",
          "title": "Verbosity",
          "type": "string"
        }
      },
      "required": [
        "ms_path"
      ],
      "title": "AntennaFlagFractionInput",
      "type": "object"
    }
  },
  "properties": {
    "params": {
      "$ref": "#/$defs/AntennaFlagFractionInput"
    }
  },
  "required": [
    "params"
  ],
  "title": "ms_antenna_flag_fractionArguments",
  "type": "object"
}
````

