# ATLAS — Autonomous Telescope & Learning Astronomy System

**Professional-grade science from your backyard.**

ATLAS is autonomous observatory control software for amateur astronomers who
want their nights to produce real scientific work — astrometry submitted to
the Minor Planet Center, photometry submitted to AAVSO and NASA Exoplanet
Watch, and transient candidates submitted to the Transient Name Server.

Five AI agents (Planner, Critic, Operator, Archivist, Oracle) — all powered by
the Anthropic Claude API — orchestrate the observatory with a clear chain of
command. The human operator stays at the top: every scientific submission
queues for human approval before it ever leaves the building.

## Documentation

- **`docs/ATLAS_Brochure.pdf`** — what ATLAS is and what it does
- **`docs/ATLAS_Operator_Manual.pdf`** — installation, setup, and operations

## Quick start

After installation, two desktop shortcuts on the observatory PC:

1. **Start ATLAS Observatory** — launches the backend server and all agents
2. **Open ATLAS Dashboard** — opens the web dashboard at `http://localhost:5000`

First launch takes you into the Setup Wizard. See Chapter 4 of the Operator
Manual for the full walkthrough.

## Install / uninstall

Run from an elevated PowerShell:

```
powershell -ExecutionPolicy Bypass -File C:\ATLAS\install.ps1
powershell -ExecutionPolicy Bypass -File C:\ATLAS\uninstall.ps1
```

The uninstaller offers three tiers (interactive prompt, or `-Mode` flag):

- `shortcuts` — remove desktop / Start Menu shortcuts + firewall rule
- `software` — also remove code and venv; **preserves the database and
  captured science data**
- `full` — remove everything, including data (automatic backup first)

## Project status

**Phase 1 (foundation) — May 2026.**

The architecture is complete and the server runs end-to-end. Workflow
pipelines (astrometry, photometry, transient detection, planetary, deep-sky)
are scaffolded with `# TODO Phase 2:` markers where the full science pipelines
plug in. See `atlas/workflows/` for the contracts.

## Architecture

```
C:\ATLAS\
├── atlas/                  Python package
│   ├── server.py          FastAPI app entry point
│   ├── config.py          Configuration loading
│   ├── security.py        Encrypted credential store
│   ├── db/                SQLAlchemy models + managers
│   ├── agents/            Five Claude-backed AI agents + message bus
│   ├── hardware/          NINA, PHD2, ASTAP, power clients
│   ├── workflows/         Six science workflow pipelines
│   ├── science/           Plate solve, photometry, astrometry, submissions
│   ├── safety/            Thresholds, pre-flight, shutdown, safe-mode
│   ├── storage/           Disk monitoring + retention
│   ├── notifications/     ntfy.sh push
│   ├── simulation/        Fake hardware for dry-run mode
│   ├── astronomy/         Visibility, moon, ephemeris, catalogs
│   ├── weather/           Open-Meteo client
│   └── api/               FastAPI routes + WebSocket
├── dashboard/             Web UI (HTML/CSS/JS) — served by FastAPI
├── catalogs/              Seasonal target catalogs
├── data/                  Runtime data (created on first launch)
│   ├── atlas.db          SQLite database
│   ├── frames/           Captured frames
│   ├── references/       Transient reference frame library
│   ├── reports/          Session reports
│   └── logs/             Runtime logs
├── docs/                  PDFs (brochure, operator manual)
├── scripts/               Build / maintenance scripts
└── tests/                 Test suite
```

## License

MIT. See `LICENSE`.
