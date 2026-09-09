# 14 — Belief State (analyst_driver, opt-in)

Read this only when the turn's brief contains a "Measured so far" /
"Working hypothesis carried from your previous turn" section — that means
`belief_state` is enabled in `config.toml` for this run, and your decision
JSON must include a `belief_state` field. If the brief has no such section,
this file does not apply; do not add the field.

## What this is, and what it is not

You are one turn in a run driven by `analyst_driver`. Every turn is a fresh
model — you do not remember previous turns except through two things the
brief gives you: a deterministic digest of what has actually been measured,
and the belief state you are about to read and revise. The digest is fact,
built by the driver from the journal, and you cannot get it wrong. The belief
state is judgment, written by a model on an earlier turn, and it can be wrong,
stale, or simply no longer relevant — treat it the way you would treat a
colleague's handoff note, not a measurement.

**Your job this turn:** read the digest and the carried belief together,
check whether the belief still holds given what is now measured, and write a
revised belief state — not an append to the old one.

## What belongs in the belief state

Interpretation the digest cannot express — durable judgment about this run
that would be expensive to re-derive from scratch next turn:

- **Antenna reliability.** "ea09 has been low-SNR since the delay solve
  (turn 3) — do not nominate as refant." Not "ea09 flag_fraction = 0.12" —
  that number is already in the digest.
- **RFI character.** "SPW 2 has persistent RFI ~1.2 GHz, excluded from
  bandpass since turn 4" — a classification (persistent vs. transient,
  localized vs. band-wide), not the channel-by-channel numbers behind it.
- **Calibrator-model exceptions and why.** "3C286 flux model set manually —
  Perley-Butler standard invalid at this frequency (turn 2)."
- **Deferred items, explicitly.** "No polarization leakage calibrator
  identified yet — not forgotten, not yet needed at this stage." A deferred
  item that silently drops off the belief state looks, to the next turn,
  like it was never noticed.
- **A decision that deviated from the obvious default, and why**, when that
  reasoning would otherwise be buried in one turn's `notes` and never
  resurface — e.g. "used solint='int' instead of 'inf': the phase calibrator
  had only 2 scans, not enough for a scan-averaged solve."

## What does not belong

- **Any value already in the digest.** If "Measured so far" reports a number,
  restating it in the belief is noise that crowds out the judgment that
  actually needs to travel forward.
- **A blow-by-blow of what ran.** That is what `ms_reduction_log` and the
  turn journal already are. The belief state is not a second log — if you
  find yourself listing every tool call, stop and ask what the *conclusion*
  from those calls was.
- **Provisional guesses not yet acted on.** A hypothesis you have not tested
  against real data is not yet a belief worth carrying; test it first, or
  say plainly that it is untested if you must mention it at all.
- **Anything the current digest already contradicts.** Don't carry a stale
  claim forward out of inertia — if turn 3's "ea09 is unreliable" is
  contradicted by a clean solve on ea09 at turn 9, drop the old claim rather
  than repeating it next to the new one.

## The reconciliation, concretely

Each turn, before writing `belief_state`:

1. Read the carried belief as a set of individual claims, not one block of
   prose.
2. For each claim, check it against "Measured so far." Three outcomes:
   - **Still holds** — keep it, worded no differently than before is fine.
   - **Contradicted or superseded** — drop or correct it. Do not keep a
     claim next to evidence that disagrees with it.
   - **Not addressed this turn** — keep it; silence in this turn's
     measurements is not evidence against it.
3. Add any new claim this turn's own work established.
4. Write the result as the full, revised `belief_state` — a few sentences to
   a short paragraph per topic (antennas, RFI, calibrator models, deferred
   items) is enough. If it is growing past a few hundred words, something in
   it has stopped being judgment and started being a log; prune toward
   conclusions.

You are not required to explain *that* you revised something — a silently
corrected belief is fine. What matters is that the next turn's belief state
is accurate against the run's actual state, not merely a copy of the last
one.
