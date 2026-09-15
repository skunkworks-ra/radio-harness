# radio-harness

`analyst_driver` is an external loop that drives a full CASA reduction turn by
turn, with no human and no long-lived agent session attached: it senses run
state from disk (`ms_workflow_status`, prior job exit codes), asks an LLM
backend for one decision (a generated calibration script, `execute=False`),
dispatches that script through a pluggable executor (local / SLURM /
HTCondor), and records the outcome to a SQLite journal before starting the
next turn from ground truth. The model and the loop never run at the same
time — a `tclean` can run for hours without holding a session open. See
[`PLAN.md`](PLAN.md) for the full design and status.

**This is a standalone repo.** It was originally built by vendoring a copy of
[radio-analyst](https://github.com/skunkworks-ra/radio-analyst)'s MCP tool
layer alongside the driver, with the intent of eventually depending on that
repo as a package. That plan changed (2026-09-15): `radio-harness` now
permanently owns its own copy of the tool layer and a driver-only fork of the
skill content, ported wholesale from `radio-analyst`'s state at the time and
maintained independently from here on — not synced automatically, not a
stand-in for a future dependency. `radio-analyst` continues to exist
separately as the interactive, standalone tool; the two repos diverge freely.

What actually lives here:

- **`src/analyst_driver/`** — the orchestration loop. This is what the repo
  exists to develop.
- **`src/ms_inspect/`, `src/ms_modify/`, `src/ms_create/`** — the MCP tool
  layer (read-only inspection, calibration/flagging, ASDM ingestion). The
  harness drives the reduction; it never reasons about the science itself —
  that reasoning lives in the skills below, not in these tools.
  - **ms-inspect** — read-only inspection and diagnostics (33 tools, port 8000)
  - **ms-modify** — calibration, flagging, and MS modification (16 tools, port 8001)
  - **ms-create** — ASDM ingestion and reduction logging (3 tools, port 8002)
- **`.claude/skills/radio-interferometry-driver/`** — the science reasoning
  (band tables, calibration solve order, calibrator selection, failure
  modes), trimmed from `radio-analyst`'s interactive skill for headless use.
- **`.claude/skills/stage-orchestration/`** — workflow-level judgment specific
  to this repo, not ported from anywhere: overall stage sequencing, whether
  the previous stage left what the next one needs, and whether a whole stage
  (not just one failed sub-step) needs redoing. Consulted before
  `radio-interferometry-driver` in a driver turn.
- **`hooks.json` + `hooks/`** — enforce that `ms_workflow_status` is actually
  called before a writing tool runs, rather than relying on a skill
  instruction the model can skip. See `HARNESS_ANALYST_SPLIT_PLAN.md` (in
  `agents-md`) for the design and how it was verified.

Built on [casatools](https://casa.nrao.edu/) and the
[Model Context Protocol](https://modelcontextprotocol.io/).

---

## Installation

### Claude Code plugin (recommended)

Installs both MCP servers, skills, and slash commands in two commands:

```bash
# Register the marketplace (once per machine)
claude plugin marketplace add https://github.com/skunkworks-ra/radio-harness

# Install the plugin
claude plugin install radio-harness@radio-harness
```

After install, the `ms-inspect`, `ms-modify`, and `ms-create` MCP servers are
registered globally, and the commands below are available in all projects.
CASA tools are installed automatically on first use (~500 MB, Linux x86_64
and macOS arm64 only).

To remove:

```bash
claude plugin uninstall radio-harness@radio-harness
```

### Local development

Use this when actively working on the plugin itself. Registers the MCP servers
directly against the local pixi environment — no plugin system involved.

```bash
git clone https://github.com/skunkworks-ra/radio-harness.git
cd radio-harness
pixi install
pixi run pip install casatools==6.7.0.31 casatasks==6.7.0.31   # first time only; ~500 MB
pixi run install-mcp
```

`install-mcp` calls `bin/install-local.sh`, which registers `ms-inspect`,
`ms-modify`, and `ms-create` via `claude mcp add --scope user` pointing
directly at `.pixi/envs/default/bin/`. Re-run after any `pixi install` that
rebuilds the environment. The script detects and removes a plugin-managed
install automatically before registering — this matters here specifically
because the `PreToolUse` write-gate hook matches on tool names, and a
plugin-managed install names tools differently
(`mcp__plugin_radio-harness_ms-modify__...`) than a direct one
(`mcp__ms-modify__...`); the gate's matcher covers both, but only one is
active per install method.

To switch back to the plugin install:

```bash
pixi run uninstall-mcp
# then follow the Claude Code plugin instructions above
```

### Claude Desktop and other MCP clients (HTTP transport)

Clone the repo, install the environment, then start the servers in HTTP mode:

```bash
git clone https://github.com/skunkworks-ra/radio-harness.git
cd radio-harness
pixi install && pixi run pip install casatools==6.7.0.31 casatasks==6.7.0.31

# Inspection server (port 8000)
RADIO_MCP_TRANSPORT=http RADIO_MCP_PORT=8000 pixi run serve

# Modification server (port 8001)
RADIO_MCP_TRANSPORT=http RADIO_MCP_PORT=8001 pixi run serve-modify

# Ingestion server (port 8002)
RADIO_MCP_TRANSPORT=http RADIO_MCP_PORT=8002 pixi run serve-create
```

Add to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ms-inspect": {
      "command": "pixi",
      "args": ["run", "--manifest-path", "/path/to/radio-harness/pixi.toml", "serve-http"]
    },
    "ms-modify": {
      "command": "pixi",
      "args": ["run", "--manifest-path", "/path/to/radio-harness/pixi.toml", "serve-modify-http"]
    }
  }
}
```

For any MCP-compatible client — point at `http://localhost:8000/mcp` (streamable HTTP).

---

## Tool inventory

The full per-tool inventory with descriptions lives in
[`DESIGN.md`](DESIGN.md) (§8 ms-inspect, §8b ms-modify, §8c ms-create). A
summary by category:

### ms-inspect — read-only inspection (33 tools)

- **Layer 1 — Orientation** (6): observation info, field list, scan list, scan
  intent summary, spectral window list, correlator config.
- **Layer 2 — Instrument sanity** (7): antenna list, baseline lengths, elevation
  vs time, parallactic angle vs time, shadowing report, flag preflight, antenna
  flag fraction.
- **Calibration inspection** (6): caltable solution stats + detail reader,
  single/library caltable plots, gaincal SNR prediction, caltable structural checks.
- **Pre-calibration inspection** (5): import/model/priorcal verification, online
  flag stats, flag summary.
- **Instrument & RFI inspection** (7): reference-antenna ranking, per-channel RFI
  stats, SpW amplitude severity, pol-cal feasibility, residual/corrected-data
  stats, phase-calibrator catalogue lookup.
- **Imaging inspection** (1): robust image RMS / peak / dynamic-range / beam.
- **Pipeline / workflow** (1): workflow state probe.

### ms-modify — calibration and flagging (16 tools)

Intent population, preflagging, prior caltables, flux models (setjy / setjy
polcal), bandpass, gaincal, polcal, fluxscale, applycal, residual and post-cal
RFI flagging, caltable autoflag, and tclean imaging. All modify tools support
`execute=False` (default) to generate a reviewable Python script without
touching the MS, and `execute=True` to run in-process.

### ms-create — ingestion (3 tools)

Pre-conversion ASDM summary, ASDM → MS import, and a per-reduction working-calls
ledger.

---

## Skills

Skills provide domain reasoning on top of tool outputs. They are loaded
automatically when the plugin is installed. Both are driver-only forks —
ported from and trimmed against `radio-analyst`'s interactive skill, not
the skill itself; see the top of this file and
`HARNESS_ANALYST_SPLIT_PLAN.md` (in `agents-md`) for why they diverge and
what was left out (`ms-simulator`, `wildcat/`).

| Skill | Purpose |
|-------|---------|
| `stage-orchestration` | Workflow-level judgment, consulted first each turn: overall stage sequencing, whether the previous stage left what the next one needs, whether a whole stage needs redoing. New content — not ported from anywhere. |
| `radio-interferometry-driver` | Stage-internal execution detail once `stage-orchestration` has named a stage — band tables, calibration solve order, calibrator selection, failure-mode recovery. |

Both skills' turns are backed by hooks (`hooks.json` + `hooks/`), not just
instructions: reading either skill forces a fresh `ms_workflow_status` call
and injects its result as context, and no `ms-modify`/`ms-create` tool call
is allowed to run until that's happened this turn.

## Slash commands

Working in a clone these are invoked as `/<name>`; installed from the
marketplace they are namespaced by the plugin, `/radio-harness:<name>`.
These are interactive commands, for exploring this repo's own copy of the
tool layer by hand — separate from `analyst_driver`'s headless turns, which
never use them.

| Command | What it does |
|---------|-------------|
| `/inspect <ms_path>` | Full Phase 1 + Phase 2 analysis with go/no-go report |
| `/precal <ms_path>` | Pre-calibration workflow (online flags → preflag → priorcals → setjy → refant → initial BP → rflag) |
| `/calibrate <ms_path>` | Full calibration solve (initial phase → delay → bandpass → gain → fluxscale → applycal) |
| `/polcal <ms_path>` | Polarisation calibration (Kcross → D-terms → Xf → applycal with parang) |
| `/image <ms_path>` | First-pass continuum/cube imaging with derived tclean parameters |
| ~~`/simulate <description>`~~ | **Stale** — it follows "the ms-simulator skill protocol," which was never ported here. Left in place, not removed, pending a decision on whether this repo needs it at all. |

## Driver usage

```bash
pixi run driver init                                    # write a default config.toml to edit
pixi run driver run --input <asdm_or_ms> --workdir <path>   # register a run if needed, then drive it
pixi run driver step --run <run_key>                    # advance one run by one turn (waits for the job)
pixi run driver status                                  # list runs, their latest turn and their owner
pixi run driver rebuild                                 # reconstruct the database from the journal
```

`config.toml` sets the backend (`claude`/`opencode`/`codex`/stub), executor
(local/SLURM/HTCondor), and declared scope for a run — science parameters
never belong there. See [`PLAN.md`](PLAN.md) for the turn loop's actual
design (sense/decide/dispatch/settle) and `DESIGN.md` for the tool layer.

---

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `RADIO_MCP_TRANSPORT` | `stdio` | `stdio` for Claude Code; `http` for remote |
| `RADIO_MCP_HOST` | `127.0.0.1` | HTTP bind address. **The HTTP transport has no authentication — do not bind beyond localhost on shared or untrusted networks** |
| `RADIO_MCP_PORT` | `8000` / `8001` / `8002` | HTTP port (inspect / modify / create) |
| `RADIO_MCP_WORKERS` | `4` | Parallel workers for FLAG column reads (cap 8) |
| `RADIO_MCP_TEST_MS` | — | Path to MS for integration tests |
| `RADIO_MCP_TEST_MS_TGZ` | — | Path to `.ms.tgz` tarball; auto-extracted by conftest.py |

---

## Development

```bash
# Unit tests (no CASA, no MS required)
pixi run test-unit

# Integration tests (requires a real MS)
RADIO_MCP_TEST_MS=/path/to/your.ms pixi run test-int

# Lint + format check
pixi run check
```

Python `>=3.12`. `casatools` and `casatasks` are PyPI-only — pixi resolves
them via pip into the conda environment.

---

## License

GPL-3.0
