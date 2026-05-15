<div align="center">

# ATLAS

### Autonomous Telescope &amp; Learning Astronomy System

***Professional-grade science from your backyard.***

**MPC · AAVSO · NASA Exoplanet Watch · TNS**

[![Phase](https://img.shields.io/badge/Phase%201-foundation%20complete-1F6FEB)](#status)
[![Python](https://img.shields.io/badge/python-3.11%2B-1F6FEB)](#)
[![License](https://img.shields.io/badge/license-MIT-1F6FEB)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2B-1F6FEB)](#)

[Full Brochure (PDF)](docs/ATLAS_Brochure.pdf) · [Operator Manual (PDF)](docs/ATLAS_Operator_Manual.pdf) · [Install](#install) · [Run](#run)

</div>

---

## A telescope that does real science

For decades, professional astronomy has run on the contributions of amateur astronomers.
The discovery alert that catches a supernova at magnitude 17 before the survey telescopes
can image the field. The thousand nights of variable-star measurements that constrain
the stellar physics of a known object. The astrometric position that confirms a newly
found asteroid is not just a moving artifact. None of these come exclusively from
billion-dollar facilities — many of them originate from a single dedicated observer
with a telescope on a backyard pier.

ATLAS exists to take that work seriously. It is autonomous observatory control software
that runs your gear like a professional facility — with the calibration discipline,
the documentation, the submission formats, and the rigor that real science demands.
Five AI agents, powered by the Anthropic Claude API, share a clear chain of command:
one plans, one watches, one decides, one processes, one researches. Together they
spend every clear night turning your captured photons into measurements that the
scientific community can use.

> ***Built for the amateur astronomer who wants their nights to count.***

ATLAS is for the operator who has spent years choosing equipment, learning collimation,
building a calibration library, and refining polar alignment. The system meets that
dedication with software that respects what an instrument is for. **Every scientific
submission stays under your control. Every decision is logged. Every measurement is
reproducible.**

---

## The five AI agents

ATLAS is not one AI. It is five — each with a defined role, a clear system prompt,
and a place in the chain of command.

| Agent | Role |
|---|---|
| **Planner** — The Strategist | Builds the nightly schedule and the NINA capture sequence. Pulls active campaigns from the database, weighs target visibility against tonight's weather, computes alt/az and airmass through the viewing window, and produces a structured plan with exposure plans per filter. Full rebuild on resume from full standby. |
| **Critic** — The Watchdog | Two continuous loops. Fast (every 90 s): guiding RMS, focus HFR, frame quality, mount tracking, camera connection. Standard (every 5 min): weather, dew margin, wind, humidity, cloud cover, calibration freshness, disk space, power source, internet, API health. Never decides; only reports. |
| **Operator** — The Authority | Final say on every autonomous decision. Reviews Critic alerts, approves Planner output, runs pre-flight before the roof opens, manages standby and resume, executes emergency shutdown. Two auto-fix attempts before escalating to the human. |
| **Archivist** — The Historian | Triggered when the session ends. Calibrates frames, stacks with Siril or AutoStakkert!4, validates FITS headers, embeds the WCS plate solution, extracts measurements, queues eligible submissions, renders the full 10-section session report. |
| **Oracle** — The Researcher | Studies the accumulated database for anomalies, runs the image-subtraction pipeline for transient candidates, cross-matches against Gaia DR3, Pan-STARRS, and the MPC, manages knowledge threads, tracks the research agenda. The agent that asks *what does the data say?* |

```
              Human Operator
                    |  approves targets, handles hardware exceptions
                    v
              Operator Agent
              /     |      \
        commands  receives  triggers
           v        |         v
       Planner   Critic    Archivist
           ^                  |  notifies new data
           +---- proposals -- v
                            Oracle
```

---

## Real science

ATLAS builds six science workflows. Each is a complete pipeline from target
acquisition through calibration, measurement, and submission.

### Asteroid & comet astrometry → IAU Minor Planet Center

Resolves an MPC designation to a live ephemeris. Computes non-sidereal tracking rates
in RA and Dec. Captures a short series sized to keep trailing below one pixel.
Plate-solves every frame with ASTAP. Measures the centroid of the moving object
against Gaia DR3 reference stars. Produces astrometric positions in the MPC 80-column
report format. Queues each observation for operator review and approval before
submission to the MPC.

*Recovery of an object from solar conjunction, follow-up on a Near-Earth Object
Confirmation Page candidate, extending the orbital arc of a Mars-crosser — all the
same workflow.*

### Variable star photometry → AAVSO International Database

Pulls the AAVSO comparison-star sequence for the target. Captures a long continuous
series in V or Sloan-r with no dithering. Autofocus runs only on filter change;
mid-series focus shifts are forbidden. Performs aperture (or PSF) photometry against
comp and check stars with proper error propagation. Outputs AAVSO Extended Format
records ready for upload to WebObs.

*Monitor Betelgeuse every clear night for a year. Watch a Mira-type long-period
variable across its cycle. Follow a cataclysmic variable into outburst.*

### Exoplanet transit photometry → NASA Exoplanet Watch / AAVSO Exoplanet Section

Given a TIC/HAT/WASP designation and a predicted transit window, ATLAS schedules
the relevant nights. Fixed-field sequence — same RA, same Dec, no slew — spanning
the transit plus pre- and post-baseline windows. **Focus locks for the duration**;
an autofocus mid-transit would inject a systematic flux change. Sub-second timing
for ingress and egress. Differential photometry, transit-model fit, light curve
in NASA Exoplanet Watch or AAVSO Exoplanet Section format.

### Supernova & transient hunting → Transient Name Server

The system maintains its own reference frame library, built from your sky and your
equipment. Every field visited is stored. After a minimum of three visits a deep
reference stack is finalised. On the next visit: plate-solve, register against
reference, run image subtraction (HOTPANTS or PyZOGY), source-extract the residual,
filter on signal-to-noise / FWHM / ellipticity, then cross-match every candidate
against Gaia DR3, Pan-STARRS, the MPC, and recent TNS reports. Only clean
cross-match results reach the submission queue.

*Real-name attribution. A supernova ID you can put on a CV.*

### Planetary imaging → Long-term solar-system monitoring

SharpCap captures SER video — typically tens of thousands of frames — and
AutoStakkert!4 performs lucky-imaging stacking. The result is high-resolution
imagery suitable for tracking weather features on Jupiter, dust storms on Mars,
ring system tilt and seasonal changes on Saturn, rotation of Venus across its
solar-system cycle.

### Deep-sky imaging → Calibrated aesthetic + photometric outputs

When no priority science workflow applies, deep-sky imaging produces calibrated
long-exposure stacks via Siril's scriptable pipeline. Pretty pictures are a
by-product of the science pipeline, not the goal — every aesthetic image carries
photometric and astrometric metadata that makes it usable for follow-up measurements.
The image of Andromeda on your wall is also a calibrated scientific dataset.

#### Target priority

**A.** Asteroid &amp; comet astrometry · **B.** Variable star + exoplanet photometry · **C1.** Supernova / transient hunting · **C2.** Planetary imaging · **D.** Deep-sky aesthetic.

---

## Imaging discipline

Real measurements require real calibration. ATLAS treats calibration and focus
quality as non-negotiable.

### Calibration library

Indexed by frame type, filter, exposure time, gain, offset, and sensor temperature.
Master bias frames. Master darks at every operating temperature × exposure pair.
Master flats per filter, per session — dawn sky flats are part of the standard
close-out routine. Every science frame is calibrated against masters that match its
acquisition parameters; the FITS header records the calibration sources used.
The Critic flags stale or temperature-drifted masters (default: 7-day window).

### Autofocus, with policy per workflow

Focus is not one-size-fits-all. ATLAS treats autofocus as a first-class workflow
parameter, with policies tuned to the science:

| Workflow | Before sequence | On filter change | Temp Δ trigger | Time interval | HFR drift |
|----------|:---:|:---:|:---:|:---:|:---:|
| Astrometry | yes | — | 2 °C | — | 20 % |
| Variable star photometry | yes | yes | 3 °C | — | — |
| Exoplanet transit | yes | no | locked | — | — |
| Transient hunting | yes | yes | 2 °C | 60 min | 15 % |
| Deep-sky imaging | yes | yes | 2 °C | 60 min | 15 % |
| Planetary | yes | no | locked | — | — |

The ZWO EAF (or any NINA-compatible focuser) is the executor; ATLAS is the brain.
Exoplanet transits **lock focus end-to-end** so no mid-transit shift contaminates
the photometric baseline.

### Plate solving and FITS metadata

Every science frame is plate-solved with ASTAP (fast, offline-capable). The WCS
solution is embedded in the FITS header of every archive frame. Headers include
`DATE-OBS` to sub-second UTC, `EXPTIME`, `FILTER`, `OBJECT`, `RA`, `DEC`, `AIRMASS`,
`TELESCOP`, `INSTRUME`, `GAIN`, `EGAIN`, `CCD-TEMP`, `FOCAL_LEN`, `PIXSCALE`,
`SITELAT`, `SITELONG`, `BAYERPAT`, `GUIDRMS`, `FWHM`, `QUALITY`.

*Anyone who later loads the frame — including you, ten years from now — has every
piece of metadata needed to reproduce the measurement.*

---

## The database — where detection happens

> ***Most observatory software writes a log. ATLAS builds a corpus.***

The database is not a record of what happened — it is the instrument the Oracle
uses to *find* what's happening. Every frame, every measurement, every decision,
every alert, every inter-agent message is stored in queryable form. Years of nights
become a single dataset you can ask questions of.

### Every detail, indexed

Twenty tables, each earning its place:

- **`frames`** — full FITS metadata, plate-solve status, WCS solution, FWHM, quality grade, calibration sources, gain, offset, temperature, filter
- **`measurements`** — epoch UTC to sub-second precision, value with uncertainty, comp stars, catalog reference for the cross-match
- **`submissions`** — destination, status, formatted payload, operator approval, external response
- **`reference_frames`** — the transient detection library, indexed by field key
- **`calibration_masters`** — every bias, dark, and flat with its acquisition parameters
- **`campaigns`** + **`campaign_targets`** — multi-night research efforts
- **`targets`** + **`knowledge_threads`** — each target's research state per kind of science
- **`decisions`** — every major agent decision with inputs, outputs, rationale, outcome
- **`alerts`** — every alert, severity, resolution
- **`agent_messages`** — full inter-agent communication audit
- **`sessions`** — every session with state, plan version, weather summary
- **`stack_products`**, **`storage_events`**, **`site_config`**, **`equipment_profile`**, **`credentials`** (encrypted), **`retention_policy`**, **`notification_config`**, **`version_info`**

### What the Oracle does with it

Six classes of continuous query:

1. **Anomaly detection** — photometric measurements deviating from historical baseline. An unusual brightening at magnitude 14.2 when the comp-relative history says 14.7. Flagged with the data trail.
2. **Long-term trends** — light curves assembled across hundreds of nights with proper error propagation. Betelgeuse's slow decline. A periodic variable's amplitude shift.
3. **Cross-target correlations** — quality drops correlated across unrelated targets in the same session usually mean an instrument problem, not real physics. The Oracle distinguishes the two.
4. **Catalog cross-matching** — every transient candidate cross-matched against Gaia DR3, Pan-STARRS, the MPC, and recent TNS reports before reaching the submission queue.
5. **Knowledge thread maturation** — data-driven state transitions: dormant → active → mature only when the success criterion is documented and met.
6. **Decision audit with hindsight** — joins every past decision to its outcome and computes a verdict. Was the threshold right? Did the call hold up?

### Built for permanence

SQLite by default; PostgreSQL migration path ready. Credentials (Anthropic API key,
AAVSO/MPC/TNS tokens, ntfy.sh topic) are encrypted at rest with AES-256-GCM via an
Argon2id-derived key. Backups and reinstalls preserve everything.

*Your science survives the next computer, the next OS, and the next decade.*

---

## Campaign-based science

Real amateur science is not single-night work. It is cadence. ATLAS treats the
multi-night research effort — the **campaign** — as a first-class object.

A campaign carries a name, a science workflow, one or more targets, a priority, a
cadence specification, a success criterion, a deadline if any, and a scientific
context note. The Planner draws from active campaigns when building each night's
schedule, weighting them by priority, urgency, and weather fit.

### Three examples

- **Betelgeuse V-band photometry — 12-month monitoring.** Cadence: every clear night. Success: 100 photometric points spanning one full year. Submission: AAVSO. Knowledge thread transitions from *active* to *mature* once the cycle is characterised.

- **TIC 1234567 transit confirmation — 4 events over 6 weeks.** Cadence: scheduled to the predicted transit windows. Success: 3 of 4 events captured with adequate baseline. Submission: NASA Exoplanet Watch. Locked focus, fixed field, no dither, sub-second timing.

- **Asteroid 2024 XY recovery — 3 nights after solar conjunction.** Cadence: first three clear nights of next visibility window. Success: astrometric positions submitted to extend the orbital arc. Submission: MPC 80-column format. Non-sidereal tracking, short exposures, Gaia DR3 reference.

### Research agenda intake

ATLAS reads external alerts from AAVSO, the Astronomer's Telegram (ATel), the MPC
Near-Earth Object Confirmation Page, and NASA Exoplanet Watch. Time-critical items
— transit windows, asteroid recovery deadlines, variable-star outburst follow-up —
are evaluated by the Oracle, prioritised by the operator, and scheduled by the Planner.

---

## Safety, resilience, and discipline

Equipment that runs unattended at 2 AM fails badly. ATLAS treats safety as
architecture, not as a feature checkbox.

### Pre-flight checklist

Before the Operator commands the roof to open, every item must pass — or the operator
must explicitly override:

- NINA reachable and responsive on the configured host:port
- PHD2 reachable on its JSON-RPC port
- Camera connected, cooling has reached setpoint within tolerance
- Focuser connected and at a non-extreme position (not pinned at min or max)
- Mount connected and at a known parked position
- Filter wheel connected (if equipped)
- Recent master darks exist matching tonight's exposure plan
- Recent master flats exist for every filter in tonight's plan
- Disk free space exceeds the configured threshold
- Weather GO for the next 60 minutes
- Internet up — or safe-autonomous mode is armed
- Anthropic Claude API responding
- Calibration freshness within the configured window (default 7 days)
- Power source nominal — not on battery near shutdown threshold

### Standby and emergency shutdown

**Light standby** pauses imaging, holds the mount, keeps the camera cooled — fast resume.

**Full standby** ramps the camera back to ambient at 5 °C/min, powers down hardware, parks the mount, closes the roof, and waits for explicit operator approval to resume.

**Emergency shutdown** fires on hard-limit weather breach, hardware failure, or operator command: stop imaging, park (verify), close the roof, warm the camera, power down hardware, save state, push a critical-priority ntfy.sh alert to the human.

### Power-source awareness

ATLAS detects the active power source via the OS UPS interface. An off-grid solar /
battery / utility / generator stack is explicitly supported. Default graceful-shutdown
trigger: 50% remaining battery or 5 minutes runtime, whichever fires first. Resume
after shutdown requires explicit operator approval.

### Offline-safe operation

Imaging, guiding, focusing, and plate solving are all local and survive an internet
drop. Catalog lookups fall back to local caches; submissions queue. If the Claude API
is unreachable, agents enter **safe-autonomous mode**: hold the current target, hold
the schedule, reject non-trivial decisions, surface the outage. *The night keeps
producing science.*

---

## The operator stays in charge

Five AI agents do the heavy lifting, but **the human is the scientist on the project.**
ATLAS is built around that asymmetry.

- **Every submission queues for human approval.** MPC astrometry, AAVSO photometry, TNS transient reports, NASA Exoplanet Watch light curves — none ever leaves the building autonomously. Every candidate appears in the Science tab with its exact payload, supporting cutouts, catalog cross-match results, and residual analysis. Approve, reject, or hold for review. Your observer code. Your name. Your scientific record.

- **Take Control toggle** in the top bar of every dashboard tab. Engage to pause agent command authority and gain direct hardware controls. Every action is logged into the session record.

- **Decision audit trail.** Every major agent decision writes a row to the `decisions` table with inputs, outputs, rationale, and outcome. The session report's Decision Audit section presents each decision in order with a hindsight verdict.

- **The 10-section session report.** Executive summary, session timeline, per-target results, plan versions, equipment performance, processing recap, error log, decision audit, campaign status, recommendations for next session. Self-documenting. Reproducible. Ready to share.

> ***The human approves. The system executes. Both are accountable.***

---

## Install

Run from an elevated PowerShell on the observatory PC:

```powershell
powershell -ExecutionPolicy Bypass -File C:\ATLAS\install.ps1
```

The installer is silent and idempotent. It:

1. Verifies / installs Python 3.11+ (via `winget` or direct download)
2. Creates a private virtual environment at `C:\ATLAS\venv`
3. Installs every Python dependency
4. Initialises the SQLite database with the complete schema
5. Opens Windows Firewall TCP port 5000 (warm-room access)
6. Creates two desktop shortcuts: **Start ATLAS Observatory**, **Open ATLAS Dashboard**
7. Adds a Start Menu folder with the same shortcuts
8. Scans for NINA, PHD2, ASTAP, Siril, AutoStakkert and reports presence

**Required:** Windows 10 / 11, NINA with the Advanced API plugin, PHD2, ASTAP, an Anthropic API key, an ntfy.sh topic.

**Optional:** Siril, AutoStakkert!4, SharpCap, AAVSO observer code, MPC observatory code, TNS API token.

## Run

After installation, two desktop shortcuts:

- **Start ATLAS Observatory** — launches the FastAPI backend + all 5 agents
- **Open ATLAS Dashboard** — opens `http://localhost:5000` in your default browser

From the warm-room PC (or any device on the LAN), point a browser at
`http://<observatory-PC-IP>:5000`. **No software install on the warm-room PC.**

On first launch, the Setup tab walks through master password → Anthropic API key →
site coordinates → NINA/PHD2 hosts → equipment profile → notifications → submission
credentials.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File C:\ATLAS\uninstall.ps1
```

Three tiers:

- **shortcuts** — remove desktop / Start Menu shortcuts + firewall rule
- **software** — also remove code and venv (preserves database and captured data)
- **full** — remove everything, with automatic backup

---

## Project structure

```
C:\ATLAS\
├── atlas/                  Python package
│   ├── server.py          FastAPI app + lifespan-managed agents
│   ├── config.py          Configuration loading
│   ├── security.py        Encrypted credential vault
│   ├── db/                SQLAlchemy schema + domain managers
│   ├── agents/            5 Claude-backed agents + bus + coordinator + prompts
│   ├── hardware/          NINA, PHD2, ASTAP, power
│   ├── workflows/         6 science workflow contracts
│   ├── science/           Submission formatters (MPC, AAVSO, TNS, NASA EO)
│   ├── safety/            Thresholds, pre-flight, shutdown, safe-mode
│   ├── storage/           Disk monitor + retention
│   ├── notifications/     ntfy.sh push
│   ├── simulation/        Fake hardware for shakedown
│   ├── astronomy/         Visibility, moon, ephemeris, catalogs (Phase 2)
│   ├── weather/           Open-Meteo client
│   └── api/               FastAPI routes + WebSocket
├── dashboard/             Web UI (HTML/CSS/JS)
├── catalogs/              Seasonal target catalogs (Phase 2)
├── data/                  Runtime: SQLite DB, frames, references, reports, logs
├── docs/                  Brochure + Operator Manual PDFs
├── scripts/               build_pdfs.py, build helpers
├── install.ps1            Silent installer
├── uninstall.ps1          Tiered uninstaller
├── start_atlas.bat        Desktop shortcut target #1
└── open_dashboard.bat     Desktop shortcut target #2
```

---

## Status

**Phase 1 — foundation complete and smoke-tested.**

The server runs end-to-end, all five agents come online, every API endpoint responds,
encryption round-trips, static dashboard assets serve, WebSocket event stream works,
DB initialises with the full 20-table schema.

Workflow pipelines (MPC ephemeris lookup, AAVSO photometric measurement, image
subtraction, etc.) are scaffolded with explicit `# TODO Phase 2:` markers and clear
contracts. The architecture is final; Phase 2 fills in the science pipelines one at
a time.

Roadmap (in priority order):

1. Asteroid / comet astrometry pipeline (priority A)
2. Variable star + exoplanet photometry pipelines (priority B)
3. Transient detection pipeline (priority C1)
4. Planetary lucky-imaging pipeline (priority C2)
5. Deep-sky stacking pipeline

---

## For the backyard scientist

You spent years choosing the mount, the optics, the camera. You learned collimation.
You built a calibration library, refined polar alignment, weathered failed adapters
and dead components. You did all of that because you wanted your nights to mean
something.

ATLAS is the software that says: *yes, they do mean something — let's make sure the
rest of the scientific community knows it.*

Every measurement that leaves this observatory carries your observer code. Every
supernova candidate you submit carries your name. Every asteroid astrometric position
you produce extends an orbital arc that professional surveys will use to plan their
next decade.

The pier in your yard is a node in a global network of dedicated observers — and
ATLAS treats it accordingly.

<div align="center">

### ATLAS

***Built for the amateur astronomer who wants their nights to count.***

</div>

---

## Documentation

- [**docs/ATLAS_Brochure.pdf**](docs/ATLAS_Brochure.pdf) — 11-page brochure (full visual layout)
- [**docs/ATLAS_Operator_Manual.pdf**](docs/ATLAS_Operator_Manual.pdf) — installation, setup, and operations manual

## License

MIT. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests welcome. The architecture is designed for Phase 2 contributions
to be additive — each science workflow has clear `plan()` and `process()` contracts, and
each submission destination has a `Submitter` ABC. Pick a workflow, fill in the pipeline,
add tests, open a PR.

The `pre-rebuild-v0.9` tag preserves the prior CLI-driven architecture if you need it
as reference.
