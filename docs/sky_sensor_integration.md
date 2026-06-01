# Sky-Sensor Integration — Design Note

*A living document. Started 2026-06-01. Captures the plan for feeding a
local real-time sky sensor (IR cloud sensor and/or all-sky camera) into
ATLAS's decision loop. No code yet — this is the map for when the
hardware arrives.*

---

## Why this exists

ATLAS's weather eyes today are Open-Meteo — a **forecast** model, not a
measurement of the actual sky above the observatory. That leaves one
blind spot: a local cloud bank the forecast missed isn't seen until it
degrades guiding (the guiding-RMS rule trips) or the verdict flips for
some other reason. The reflex arc is sharp on what it can measure and
blind on what it can't.

A local sky sensor closes that gap. It gives ATLAS **ground truth** about
the sky directly overhead, in real time, so the existing adaptation
reflexes (pause / truncate / safe-shutdown) fire on what's *actually*
happening, not on a model's best guess.

The key architectural fact that makes this cheap to add: **Right Now is a
view fed by state slots, and the confidence rules read Right Now.** A new
sensor doesn't touch the decision logic. You wire the *eye*; the
*reflexes* are already built.

---

## Two tiers of hardware

There are two sensors worth considering, and they serve different jobs.
Start with the cheap one — it's the better *decision* sensor per dollar.

### Tier 1 — IR sky-temperature sensor (recommended first)
- **Example:** MLX90614 (or MLX90640 for a small array) pointed at zenith,
  read by a Raspberry Pi or microcontroller. ~$15–30.
- **Signal:** clear sky radiates very cold (large sky-minus-ambient
  temperature delta); cloud radiates warm (small delta). The delta is a
  near-instant cloud proxy — **a clean number, no image processing.**
- **Job:** drives the *reflexes*. Cloud-cover input to the verdict.
- **Why first:** highest signal-to-cost for autonomous decisions. Drops
  straight into a state slot as a verdict input.

### Tier 2 — All-sky camera (add later for the view + motion)
- **Example:** ZWO ASI all-sky, or a Pi HQ camera + fisheye. ~$150–400.
- **Signal:** a full-hemisphere image. Two useful extractions:
  1. **Cloud-cover %** — star-count vs expected, or brightness/contrast
     analysis. Requires real image processing.
  2. **Cloud motion vector** — frame-to-frame diff. This is the unique
     value: you can *see a bank moving in from the west and pre-empt it*
     before it reaches zenith. Predictive, not just reactive.
- **Job:** gives the human a picture, and gives ATLAS look-ahead.

**Dream setup:** both. IR sensor drives the reflexes; all-sky camera
gives the view and the motion-prediction. But Tier 1 alone delivers
~80% of the adaptive benefit. Tier 2 is an upgrade, not a prerequisite.

---

## The integration seam (four small pieces)

This mirrors how the existing weather path works. None of it touches the
verdict watcher or the confidence rules — they already react to Right Now.

### 1. Ingest
A small background poller, same shape as the existing weather poller.
- IR sensor: read temp delta every 30–60 s → compute cloud proxy.
- All-sky cam: capture frame → analyze → cloud % + motion vector.
Could be its own asyncio loop, or fold into the Critic's fast loop so the
cadence already tightens when imaging.

### 2. State slot
New slot on `_ObservatoryState`, mirroring `WeatherAssessment`:

```python
@dataclass
class SkyConditions:
    observed_at: str            # ISO
    source: str                 # "ir_sensor" | "allsky_cam" | "fused"
    cloud_pct: float | None     # 0–100, best estimate
    sky_ambient_delta_c: float | None   # IR: large = clear, small = cloud
    transparency: str | None    # "excellent" | "good" | "poor" (star count)
    cloud_motion_deg: float | None      # all-sky: bearing clouds moving FROM
    trend: str | None           # "clearing" | "stable" | "deteriorating"
    sensor_healthy: bool = True # see "sensor health" below
```

Plus `set_sky_conditions(...)` / `get_sky_conditions()`.

### 3. Right Now
One new field in the **situational** layer of `get_right_now()`:

```python
"sky_sensor": {
    "cloud_pct": ...,
    "trend": ...,
    "source": ...,
    "sensor_healthy": ...,
},
```

Because Right Now is a view that reads slots, this is ~5 lines. It then
appears automatically in the dashboard's Right Now bar, the LLM's
`get_right_now` tool, and every agent's Python access — one source of
truth, three callers, no extra plumbing.

### 4. Critic fusion (the important part)
The Critic already produces the `WeatherAssessment` that drives the
verdict. The sky sensor becomes another input it fuses, with one rule:

> **Local ground-truth outranks the forecast.** When the sensor and the
> Open-Meteo forecast disagree about cloud, the *sensor* wins for the
> verdict. The forecast still drives look-ahead (dew, wind trend hours
> out); the sensor drives "what is the sky doing right now."

That single priority rule is what closes the blind spot.

---

## What it feeds (no new decision code needed)

Once cloud data is flowing into the slot and surfaced in Right Now, the
**existing** reflexes fire on it:

- Sensor says cloudy → Critic lowers the verdict → verdict watcher's
  `nogo_sustained` rule → **pause** (hysteresis already applies).
- Sensor clears → verdict returns GO → `go_after_pause` rule → **resume**
  where execution stopped.
- Hard cloud-out (e.g. cloud_pct > 90 sustained) → existing hard-stop
  pre-empt → **safe-shutdown**.

Optional *new* confidence rules later, once we trust the sensor:
- All-sky motion vector shows a bank inbound from the west + ETA < 20 min
  → **truncate** the current slot early / pre-position, before it hits
  zenith. (This is the predictive win Tier 2 unlocks.)

---

## Guardrails (so a flaky sensor doesn't cause thrash)

The doctrine says "don't replan on noise." A new sensor must respect it:

- **Hysteresis.** Cloud crossings use the same sustained-past-threshold
  logic the verdict already uses. One warm IR reading is not a NO-GO.
- **Sensor health.** A frozen/disconnected sensor must *fail safe, not
  fail loud*: if readings stop updating or go out of physical range, set
  `sensor_healthy = False`, fall back to the forecast, and file an
  advisory — never let a dead sensor fabricate a "clear" verdict that
  keeps the roof open into a storm.
- **Fusion, not replacement.** The forecast still owns look-ahead (dew,
  wind hours out). The sensor owns *now*. They cooperate.
- **Calibration.** IR sky-ambient delta thresholds are site- and
  season-dependent (humidity shifts the clear-sky baseline). Plan to
  learn the clear/cloudy delta from the first few nights rather than
  hard-coding it — a natural fit for Confidence Layer 2 (history
  patterns).

---

## Build order (when the hardware arrives)

1. **Ingest poller + `SkyConditions` slot** — get a number flowing, log
   it, prove it tracks reality across a few nights (no decisions yet).
2. **Surface in Right Now** — dashboard field + LLM tool, read-only.
   Watch it next to the forecast; confirm it agrees when clear and
   disagrees when the forecast is wrong.
3. **Critic fusion** — wire the local-ground-truth-outranks-forecast
   rule into the verdict. Now the existing reflexes act on it.
4. **Sensor-health fail-safe** — before trusting it autonomously.
5. **(Tier 2) All-sky cloud-motion pre-emption rule** — the predictive
   truncate, once the camera + motion extraction are in.
6. **(Later) Layer-2 calibration** — learn the site's clear/cloudy
   thresholds from accumulated session history.

---

## One-line summary

Wire the eye, the reflexes are already built. IR sensor first for the
decisions, all-sky camera later for the view and the look-ahead, and in
both cases the data lands in a slot, surfaces in Right Now, and the
Critic fuses it with local truth outranking the forecast.
