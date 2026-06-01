# Roll-Off Roof Control — Design Note & Hardware Guide

*A living document. Started 2026-06-01. Captures how ATLAS commands a
roll-off roof (ROR), the hardware to build it for autonomous operation,
and the safety interlocks that are non-negotiable. The physical roof is
still under construction; this is the map for wiring it to ATLAS when the
rails are down and the motor is mounted.*

---

## Why this is the keystone

Today ATLAS's safe-shutdown sequence parks the mount, stops the guider,
and warms the camera — but it can only *advise* you to close the roof
(`roof_mode = manual`). The moment the roof is motorized and wired to a
controller ATLAS can command, safe-shutdown becomes physically complete.
"We would rather lose a night than damage the observatory" stops being a
sentence in a markdown file and becomes a thing the system can do at 3 AM
while you sleep. **Mechanizing the roof is what turns ATLAS from a
monitoring system into an autonomous one.**

It is also the highest-consequence subsystem in the whole build. A roof
that closes on a telescope still pointed at the sky destroys the OTA. So
this document leads with safety, not convenience.

---

## What ATLAS already has

- **`equipment_profile.roof_mode`** — `"nina" | "custom" | "manual"`
  (default `manual`). The 3-way switch is already in the schema.
- **`equipment_profile.roof_driver_module`** — a string field reserved as
  the import path for a custom Python roof driver (the `custom` path).
- **`NinaClient.dome_open()` / `dome_close()`** — POST to NINA's
  `/equipment/dome/open|close`. The command path exists.
- **`SafeShutdownSequence` step 5** closes the roof, and it runs *after*
  the mount-park step (step 3). The ordering intent is correct.

## What is missing (and must be built before autonomous close)

1. **Roof position read-back.** `dome_close()` fires a command and assumes
   success. There is no `dome_status()` / limit-switch sensing. ATLAS
   must *know* the roof is open or closed, never infer it from "I sent
   the command."
2. **A hard mount-park interlock.** Today step 5 runs after step 3 by
   *ordering* only. If the park step times out or errors, the close still
   fires. The close path must *verify* the mount is parked (read it back)
   and refuse to move the roof otherwise.
3. **A `RoofController` abstraction** so the `nina` and `custom` paths
   share one open/close/get_state interface.
4. **A preflight gate** ("roof reachable, responds, position known").
5. **Roof state in Right Now** so the reflexes + dashboard see it.

---

## The three control paths (mapped to `roof_mode`)

### `nina` — through an ASCOM driver (recommended)
If your roof controller ships an ASCOM dome/roof driver (most commercial
controllers do), NINA talks ASCOM and ATLAS talks NINA. You already have
`dome_open()` / `dome_close()`. This is the least custom code: if the
controller speaks ASCOM, the command path is essentially done — you only
need to add the status read-back + interlock.

### `custom` — direct relay/network box
For a fully DIY controller with no ASCOM driver. `roof_driver_module`
points at a small Python module exposing `open()`, `close()`, `status()`.
ATLAS imports it and calls it directly — no NINA in the loop. Use this
for a bare USB relay board, a network relay (Shelly/ESP32 HTTP), or Pi
GPIO that you drive yourself.

### `manual` — advisory only (current default)
ATLAS parks + warms, then notifies you to close the roof by hand. Safe,
but not autonomous.

---

## Hardware architecture (the four blocks)

```
   [ Weather safety ]        [ Roof controller ]        [ Drive motor ]
   rain + cloud + wind  -->  brain: relays + sensor  -->  moves the roof
   (RG-11 / CloudWatcher)    inputs + ASCOM driver        on its rails
            |                        |   ^
            | hardware panic-close   |   | position + park sensing
            +------------------------+   +---[ limit switches +
                                              mount-park sensor ]
```

1. **Drive motor** — physically moves the roof.
2. **Roof controller** — the brain: triggers the motor, reads sensors,
   presents to ATLAS (ASCOM or custom).
3. **Position + park sensing** — limit switches (open/closed) + a
   mount-parked sensor feeding the controller.
4. **Weather safety** — rain/cloud/wind detector, ideally with a
   hardware path to force-close independent of the PC.

---

## Hardware recommendations for autonomous operation

The single most important property for *unattended* safety: **the
controller should enforce the mount-park interlock and rain-close in
hardware, independent of the PC.** If the software hangs, you still must
not close on the scope or sit open in the rain.

Prices are rough and vary; verify current availability and size the motor
to *your* roof's weight and travel length.

### Recommended stack — "purpose-built for unattended ROR"

| Block | Recommendation | Why |
|---|---|---|
| **Controller** | **Talon6 ROR controller** (Astrogene1000-class purpose-built ROR board) | Designed for unattended roll-offs. Hardware interlocks: won't close unless mount-park input is satisfied; auto-closes on rain/power-loss; reads open/closed limits. Ships an ASCOM driver → `roof_mode=nina`. This is the safest "it's built for exactly this" option. |
| **Drive** | **Commercial sliding-gate opener** (rack-and-pinion, sized to roof weight) | Built for outdoor duty, soft start/stop, weatherproofed, own internal limits. The ROR community's most common, most robust drive. Garage-door operators work but seal/geometry are worse. |
| **Position sensing** | **Heavy-duty weather-sealed limit/reed switches** at fully-open + fully-closed | Mandatory ground truth. Never trust "command sent." |
| **Mount-park interlock** | **Physical park sensor** (magnet on mount + reed switch) wired to the controller, **plus** the software park check | Belt-and-suspenders. The hardware sensor protects the scope even if software lies. |
| **Weather safety** | **AAG CloudWatcher** (cloud + rain + wind, ASCOM Safety Monitor) | One unit gives rain-close safety **and** the local cloud sensing from `sky_sensor_integration.md`. Doubles as the sky sensor we designed. Its safety relay can wire to the controller for hardware panic-close. |
| **Power** | **UPS on PC + controller** | A power blip must not strand the roof mid-travel or kill the rain-close path. Decide the defined state on total power loss. |

### Budget / DIY alternative — "open-source, cheaper, more work"

| Block | Alternative | Notes |
|---|---|---|
| **Controller** | **RollOffIno** (Arduino + relay board, open-source sketch + ASCOM/INDI driver) | Wire relays + limit switches + a mount-park input to an Arduino, flash RollOffIno, and it presents as an ASCOM roof → `roof_mode=nina` (or `custom`). You own the interlock logic in the sketch. ~parts cost. Great if you like building. |
| **Weather safety** | **Hydreon RG-11** rain sensor | The de-facto standard optical rain sensor, relay output. Wire the relay directly to the controller's "close now" input for a PC-independent hardware panic-close. Pair separately with the IR sky sensor from the other doc if you skip the CloudWatcher. |
| Drive / position / park / power | same as above | The motor, limits, park sensor, and UPS choices don't change. |

### The synergy worth noting
If you buy the **AAG CloudWatcher**, it satisfies *both* this document's
weather-safety block *and* the `sky_sensor_integration.md` cloud-sensing
plan in one purchase — cloud, rain, and wind, presented to ATLAS as an
ASCOM **Safety Monitor** that the Critic reads as a verdict input. That is
probably the highest-leverage single buy for autonomous operation.

---

## Non-negotiable safety interlocks

These are not optional. Roof + telescope = the most expensive collision
in the observatory.

1. **Position sensing is mandatory.** Two limit switches minimum
   (fully-open, fully-closed). The roof state is what the *sensors* say,
   never what the command was.
2. **Mount-park interlock is a HARD GATE.** The roof must refuse to close
   unless the mount is *confirmed* parked (read the park state back) and
   the OTA physically clears the roof envelope. Enforce in software AND,
   ideally, in the controller hardware. If park can't be confirmed →
   stop, do **not** close, raise a critical alert.
3. **Rain/weather force-close still honors the park gate** — but park is
   fast, so the emergency path is park → confirm → close, with a
   watchdog. A rain sensor wired *directly* to the controller (PC-
   independent) is the recommended hardware backstop: it can trigger the
   controller's own park-then-close logic even if ATLAS or the PC is dead.
4. **Defined power-loss behavior.** Decide what the roof does on mains/PC
   loss (ideally: stays put, doesn't fall open) and put the controller on
   a UPS. Keep a manual way to close it by hand.
5. **Manual override the software cannot fight** — a physical clutch /
   disconnect so you can move the roof by hand, and a hardware stop.

---

## The `RoofController` abstraction (to build when wired)

```python
class RoofState(str, Enum):
    OPEN = "open"; CLOSED = "closed"; MOVING = "moving"
    UNKNOWN = "unknown"; ERROR = "error"

class RoofController:
    """One interface, two backends (nina/ASCOM or custom module)."""
    async def open(self) -> RoofState: ...
    async def close(self) -> RoofState: ...
    async def get_state(self) -> RoofState: ...   # reads limit switches
    async def is_reachable(self) -> bool: ...
```

- `nina` backend wraps `NinaClient.dome_open/close` + a new
  `dome_status()` read-back.
- `custom` backend imports `roof_driver_module` and calls its
  `open/close/status`.
- The **close path lives above this** and enforces the park gate:
  ```
  verify mount parked  ->  roof.close()  ->  poll get_state() until
  CLOSED or timeout  ->  on failure: stop + critical alert
  ```

### Integration points
- **Preflight:** add a "roof" gate — reachable, responds, position known.
  A roof in UNKNOWN/ERROR is a NO-GO for opening.
- **Right Now (situational layer):** add `"roof": {state, reachable,
  last_moved_at}` so the reflexes + dashboard see it.
- **Safe-startup:** open the roof *before* unparking, with the same
  read-back discipline (confirm OPEN before slewing).
- **Safe-shutdown:** replace the bare `dome_close()` at step 5 with the
  park-gated close-and-verify above.
- **ASCOM Safety Monitor:** if the weather device presents as a Safety
  Monitor, the Critic reads "is it safe?" as a verdict input — the same
  local-ground-truth-outranks-forecast fusion from the sky-sensor doc.

---

## Build order (when the hardware is mounted)

1. **Wire + bench-test the controller** standalone — open/close/limits/
   park sensor all reading correctly, *before* ATLAS touches it.
2. **`RoofController` + `dome_status()` read-back** — prove ATLAS can read
   the true state.
3. **Park-gated close-and-verify** — the hard interlock. Test exhaustively
   in daylight with the scope deliberately unparked: the roof must REFUSE.
4. **Preflight gate + Right Now field** — surface roof state everywhere.
5. **Safe-startup roof-open** — confirm OPEN before any slew.
6. **Weather force-close path** — rain → park → close, plus the hardware
   backstop.
7. **Switch `roof_mode` from `manual` to `nina`/`custom`** — only after
   1–6 pass. This is the line ATLAS crosses into true autonomy.

---

## Failure modes to test before trusting it unattended

| Failure | Required behavior |
|---|---|
| Close commanded, mount NOT parked | Refuse to close; critical alert. |
| Close commanded, roof jams mid-travel | Detect via limit timeout; stop motor; critical alert; do NOT retry blindly. |
| Limit switch disconnected / lying | State = UNKNOWN; treat as unsafe; alert. |
| PC/software hangs during rain | Hardware rain path parks (if wired) + closes independent of PC. |
| Power loss mid-motion | Roof holds position; UPS keeps controller alive long enough to finish/alert. |
| Park sensor + ASCOM park disagree | Trust the *more conservative* (assume NOT parked); refuse close; alert. |

---

## One-line summary

Motorize the roof, give it a controller that enforces the mount-park
interlock and rain-close in hardware (Talon6-class, or RollOffIno DIY),
sense position with real limit switches, and let ATLAS command it through
the `nina` path with a status read-back and a park-gated close-and-verify.
Buy an AAG CloudWatcher and it covers rain safety *and* the cloud sensing
from the sky-sensor doc in one unit. The roof is the keystone: it is the
moment the safety doctrine gets teeth.
