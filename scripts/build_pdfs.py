"""Generate ATLAS Brochure (v2) and Operator Manual PDFs.

v2 brochure: 10 pages, deep treatment of every workflow, calibration
discipline, campaign model, safety, and the operator's place at the top
of the chain. Built for the amateur astronomer who takes science seriously.
"""
from reportlab.lib.pagesizes import letter, LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    KeepTogether, Flowable, ListFlowable, ListItem,
)
from reportlab.pdfgen import canvas
from reportlab.lib import colors

# Brand colors
NAVY = HexColor("#0B1F3A")
ACCENT = HexColor("#1F6FEB")
SOFT = HexColor("#5B7CA8")
GOLD = HexColor("#E0B84B")
LIGHT_BG = HexColor("#F5F8FC")
DARK_TEXT = HexColor("#0F172A")
GREY_TEXT = HexColor("#475569")
RULE = HexColor("#CBD5E1")


class BrochureCover(Flowable):
    def __init__(self, width, height):
        Flowable.__init__(self)
        self.width = width
        self.height = height
    def draw(self):
        c = self.canv
        c.setFillColor(NAVY)
        c.rect(-0.75*inch, -0.75*inch, self.width + 1.5*inch, self.height + 1.5*inch, fill=1, stroke=0)
        import random
        random.seed(7)
        c.setFillColor(white)
        for _ in range(160):
            x = random.uniform(-0.5*inch, self.width + 0.5*inch)
            y = random.uniform(0, self.height)
            r = random.choice([0.3, 0.5, 0.7, 0.9, 1.2, 1.6])
            c.circle(x, y, r, fill=1, stroke=0)
        c.setStrokeColor(GOLD)
        c.setLineWidth(1.4)
        c.line(0, self.height*0.40, self.width, self.height*0.40)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 110)
        c.drawCentredString(self.width/2, self.height*0.60, "ATLAS")
        c.setFont("Helvetica", 13)
        c.setFillColor(GOLD)
        c.drawCentredString(self.width/2, self.height*0.53,
            "Autonomous Telescope & Learning Astronomy System")
        c.setFont("Helvetica-Oblique", 15)
        c.setFillColor(white)
        c.drawCentredString(self.width/2, self.height*0.30,
            "Professional-grade science from your backyard.")
        c.setFont("Helvetica", 11)
        c.setFillColor(GOLD)
        c.drawCentredString(self.width/2, self.height*0.24,
            "MPC  -  AAVSO  -  NASA Exoplanet Watch  -  TNS")
        c.setFont("Helvetica", 9.5)
        c.setFillColor(SOFT)
        c.drawCentredString(self.width/2, 0.5*inch,
            "Version: Design Phase 1.0  -  May 2026")
    def wrap(self, *args):
        return self.width, self.height


def section_band(canv, doc, title):
    w, h = LETTER
    canv.setFillColor(NAVY)
    canv.rect(0, h-0.7*inch, w, 0.7*inch, fill=1, stroke=0)
    canv.setFillColor(white)
    canv.setFont("Helvetica-Bold", 16)
    canv.drawString(0.75*inch, h-0.45*inch, title)
    canv.setFillColor(GOLD)
    canv.setFont("Helvetica-Bold", 10)
    canv.drawRightString(w-0.75*inch, h-0.45*inch, "ATLAS")
    canv.setFillColor(GREY_TEXT)
    canv.setFont("Helvetica", 8)
    canv.drawString(0.75*inch, 0.4*inch, "ATLAS - Autonomous Observatory Software")
    canv.drawRightString(w-0.75*inch, 0.4*inch, f"Page {doc.page}")


def _styles():
    s = getSampleStyleSheet()
    return {
        "h_section": ParagraphStyle("hSec", parent=s["Heading1"],
            fontName="Helvetica-Bold", fontSize=22, textColor=NAVY,
            spaceAfter=10, spaceBefore=4, leading=26),
        "h_sub": ParagraphStyle("hSub", parent=s["Heading2"],
            fontName="Helvetica-Bold", fontSize=13.5, textColor=ACCENT,
            spaceAfter=4, spaceBefore=10, leading=16),
        "h_item": ParagraphStyle("hItem", parent=s["BodyText"],
            fontName="Helvetica-Bold", fontSize=11.5, textColor=ACCENT,
            spaceAfter=2, spaceBefore=8, leading=14),
        "body": ParagraphStyle("body", parent=s["BodyText"],
            fontName="Helvetica", fontSize=10.5, textColor=DARK_TEXT,
            leading=15, spaceAfter=6, alignment=TA_JUSTIFY),
        "body_dense": ParagraphStyle("bdense", parent=s["BodyText"],
            fontName="Helvetica", fontSize=10, textColor=DARK_TEXT,
            leading=14, spaceAfter=4, alignment=TA_JUSTIFY),
        "lead": ParagraphStyle("lead", parent=s["BodyText"],
            fontName="Helvetica", fontSize=12, textColor=DARK_TEXT,
            leading=17, spaceAfter=10, alignment=TA_JUSTIFY),
        "bullet": ParagraphStyle("bul", parent=s["BodyText"],
            fontName="Helvetica", fontSize=10.5, leading=14,
            textColor=DARK_TEXT, leftIndent=14, bulletIndent=4, spaceAfter=3),
        "closing": ParagraphStyle("closing", parent=s["BodyText"],
            fontName="Helvetica-Oblique", fontSize=14, textColor=NAVY,
            alignment=TA_CENTER, leading=20, spaceAfter=10),
        "pull": ParagraphStyle("pull", parent=s["BodyText"],
            fontName="Helvetica-Oblique", fontSize=13, textColor=NAVY,
            alignment=TA_CENTER, leading=18, spaceAfter=12, spaceBefore=8,
            leftIndent=20, rightIndent=20),
    }


def make_brochure(filename):
    doc = SimpleDocTemplate(filename, pagesize=LETTER,
                            leftMargin=0.75*inch, rightMargin=0.75*inch,
                            topMargin=0.9*inch, bottomMargin=0.7*inch,
                            title="ATLAS Brochure",
                            author="ATLAS Project")
    st = _styles()
    story = []
    page_w, page_h = LETTER

    # ---- PAGE 1: COVER ----
    story.append(BrochureCover(page_w - 2.0*inch, page_h - 2.2*inch))
    story.append(PageBreak())

    # ---- PAGE 2: A Telescope That Does Real Science ----
    story.append(Paragraph("A Telescope That Does Real Science", st["h_section"]))
    story.append(Paragraph(
        "For decades, professional astronomy has run on the contributions of "
        "amateur astronomers. The discovery alert that catches a supernova at "
        "magnitude 17 before the survey telescopes can image the field. The "
        "thousand nights of variable-star measurements that constrain the "
        "stellar physics of a known object. The astrometric position that "
        "confirms a newly found asteroid is not just a moving artifact. None "
        "of these come exclusively from billion-dollar facilities -- many of "
        "them, in fact, originate from a single dedicated observer with a "
        "telescope on a backyard pier.", st["body"]))
    story.append(Paragraph(
        "ATLAS exists to take that work seriously. It is autonomous "
        "observatory control software that runs your gear like a professional "
        "facility -- with the calibration discipline, the documentation, the "
        "submission formats, and the rigor that real science demands. Five AI "
        "agents, powered by the Anthropic Claude API, share a clear chain of "
        "command: one plans, one watches, one decides, one processes, one "
        "researches. Together they spend every clear night turning your "
        "captured photons into measurements that the scientific community "
        "can use.", st["body"]))
    story.append(Paragraph(
        "&#8220;Built for the amateur astronomer who wants their nights to count.&#8221;",
        st["pull"]))
    story.append(Paragraph(
        "ATLAS is for the operator who has spent years choosing equipment, "
        "learning collimation, building a calibration library, and refining "
        "polar alignment. The system meets that dedication with software "
        "that respects what an instrument is for. Every scientific submission "
        "stays under your control. Every decision is logged. Every measurement "
        "is reproducible. And every clear night, the system asks the same "
        "question your professional colleagues ask: what work can we contribute "
        "tonight that adds to the shared map of the universe?", st["body"]))
    story.append(PageBreak())

    # ---- PAGE 3: The Five AI Agents ----
    story.append(Paragraph("The Five AI Agents", st["h_section"]))
    story.append(Paragraph(
        "ATLAS is not one AI. It is five -- each with a defined role, a clear "
        "system prompt, and a place in the chain of command. Specialisation "
        "and structure are what turn a language model into an operational tool.",
        st["lead"]))

    agents = [
        ("PLANNER  -  The Strategist",
         "Builds the nightly schedule and the NINA capture sequence. Pulls "
         "active campaigns from the database, weighs target visibility against "
         "tonight's weather, computes alt/az and airmass through the viewing "
         "window, scores priorities, and produces a structured plan with start "
         "and end times for each target, exposure plans per filter, and "
         "dither / no-dither flags. On request from the Operator -- when "
         "conditions change, a target is compromised, or the system resumes "
         "from full standby -- it produces a complete rebuild rather than a "
         "patch."),
        ("CRITIC  -  The Watchdog",
         "Runs two continuous loops. The fast loop (every 90 seconds) checks "
         "guiding RMS, focus HFR, frame quality grade, mount tracking, and "
         "camera connection. The standard loop (every 5 minutes) checks "
         "weather, dew margin, wind, humidity, cloud cover, calibration "
         "freshness, disk space, power source, internet, and API health. "
         "Critic never decides; it reports to the Operator with an alert "
         "severity, a one-line message, and structured data."),
        ("OPERATOR  -  The Authority",
         "Final say on every autonomous decision. Reviews Critic alerts, "
         "approves or revises Planner output, runs the pre-flight checklist "
         "before the roof opens, manages standby and resume, executes the "
         "emergency shutdown sequence, orchestrates software transitions "
         "(NINA &#8596; SharpCap), and decides what to escalate to the human. "
         "Two auto-fix attempts before escalation. Never autonomously submits "
         "science -- every submission queues for human approval."),
        ("ARCHIVIST  -  The Historian",
         "Triggered when the session ends. Calibrates every science frame "
         "against the active masters, stacks deep-sky data with Siril, "
         "processes planetary video with AutoStakkert!4, validates FITS "
         "headers, embeds the WCS plate solution, extracts photometric and "
         "astrometric measurements, queues eligible submissions to MPC / "
         "AAVSO / TNS / NASA Exoplanet Watch, and renders the full 10-section "
         "HTML session report. Then notifies the Oracle that new data is ready."),
        ("ORACLE  -  The Researcher",
         "Studies the accumulated database for anomalies, runs the image-"
         "subtraction pipeline for transient candidates, cross-matches "
         "potential discoveries against Gaia DR3, Pan-STARRS, and the MPC, "
         "manages each target's knowledge threads (dormant &#8594; active "
         "&#8594; mature), tracks the research agenda from AAVSO and ATel "
         "alerts, and feeds new candidate targets back to the Planner. The "
         "Oracle is what makes ATLAS more than an imaging system: it is the "
         "agent that asks &#8220;what does the data say?&#8221;"),
    ]
    for name, desc in agents:
        story.append(Paragraph(name, st["h_item"]))
        story.append(Paragraph(desc, st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 4: Real Science Part 1 ----
    story.append(Paragraph("Real Science  -  Part 1", st["h_section"]))
    story.append(Paragraph(
        "ATLAS builds six science workflows. Each one is a complete pipeline "
        "from target acquisition through calibration, measurement, and "
        "submission. The first three are the work where amateurs reliably "
        "contribute to professional astronomy today.",
        st["lead"]))

    story.append(Paragraph(
        "ASTEROID &amp; COMET ASTROMETRY  &#8594;  IAU Minor Planet Center",
        st["h_item"]))
    story.append(Paragraph(
        "Resolves an MPC designation to a live ephemeris. Computes the "
        "non-sidereal tracking rates in RA and Dec. Commands the mount to "
        "track at those rates. Captures a short series sized to keep trailing "
        "below one pixel. Plate-solves every frame with ASTAP. Measures the "
        "centroid of the moving object against Gaia DR3 reference stars. "
        "Produces astrometric positions in the MPC 80-column report format. "
        "Queues each observation as a Submission row with destination MPC, "
        "status QUEUED. The operator reviews the residuals, approves, and "
        "the submission is sent. Recovery of an object from solar conjunction, "
        "follow-up on a Near-Earth Object Confirmation Page candidate, or "
        "extending the orbital arc of a Mars-crosser -- all the same workflow.",
        st["body_dense"]))

    story.append(Paragraph(
        "VARIABLE STAR PHOTOMETRY  &#8594;  AAVSO International Database",
        st["h_item"]))
    story.append(Paragraph(
        "Pulls the AAVSO comparison-star sequence for the target. Captures a "
        "long continuous series in V or Sloan-r with no dithering -- dithering "
        "would inject systematics into a differential photometry baseline. "
        "Autofocus runs only on filter change; mid-series focus shifts are "
        "forbidden. Performs aperture (or PSF) photometry against the comp "
        "and check stars. Computes differential magnitude with proper error "
        "propagation. Outputs AAVSO Extended Format records ready for upload "
        "to WebObs. Multi-night campaigns are first-class: monitor Betelgeuse "
        "every clear night for a year, watch a Mira-type long-period variable "
        "across its cycle, follow a cataclysmic variable into outburst. The "
        "campaign tracks its own cadence and progress.",
        st["body_dense"]))

    story.append(Paragraph(
        "EXOPLANET TRANSIT PHOTOMETRY  &#8594;  NASA Exoplanet Watch / AAVSO Exoplanet Section",
        st["h_item"]))
    story.append(Paragraph(
        "Given a TIC/HAT/WASP designation and a predicted transit window, "
        "ATLAS schedules the relevant nights. The Planner builds a fixed-"
        "field sequence -- same RA, same Dec, no slew -- spanning the transit "
        "plus pre- and post-baseline windows. Focus locks for the duration; "
        "an autofocus mid-transit would inject a systematic flux change. "
        "Acquires the precise sub-second timing required for ingress and "
        "egress measurement. The Archivist performs differential photometry, "
        "fits a transit model, and produces a light curve in NASA Exoplanet "
        "Watch or AAVSO Exoplanet Section format. The operator reviews the "
        "fit and submits. Your data is part of the public record of that "
        "exoplanet's transit timing.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 5: Real Science Part 2 ----
    story.append(Paragraph("Real Science  -  Part 2", st["h_section"]))

    story.append(Paragraph(
        "SUPERNOVA &amp; TRANSIENT HUNTING  &#8594;  Transient Name Server",
        st["h_item"]))
    story.append(Paragraph(
        "The system maintains its own reference frame library, built from "
        "your sky and your equipment. Every field visited is stored. After a "
        "minimum of three visits a deep reference stack is finalised. On the "
        "next visit, ATLAS plate-solves the new frame, registers it against "
        "the reference, runs image subtraction (HOTPANTS or PyZOGY), and "
        "extracts sources from the residual. Each candidate is filtered on "
        "signal-to-noise, FWHM consistency, and ellipticity. Then comes the "
        "discipline that separates discovery from embarrassment: the Oracle "
        "cross-matches every candidate against Gaia DR3, Pan-STARRS, the "
        "MPC, and recent TNS reports. Only candidates with a clean cross-"
        "match result reach the Science tab's submission queue. The operator "
        "reviews the cutout, the reference comparison, and the catalog "
        "results, then approves submission to the Transient Name Server. "
        "Real-name attribution. A supernova ID you can put on a CV.",
        st["body_dense"]))

    story.append(Paragraph(
        "PLANETARY IMAGING  &#8594;  Long-term solar-system monitoring",
        st["h_item"]))
    story.append(Paragraph(
        "The Operator launches SharpCap with the correct ROI, gain, and "
        "frame rate for the target. ATLAS captures SER video -- typically "
        "tens of thousands of frames -- then hands off to AutoStakkert!4 for "
        "lucky-imaging stacking. The result is high-resolution imagery "
        "suitable for tracking weather features on Jupiter, dust storms on "
        "Mars, ring system tilt and seasonal changes on Saturn, and rotation "
        "of Venus across its solar-system cycle. With consistent observing "
        "cadence, these become a long-term monitoring record of our own "
        "solar system.",
        st["body_dense"]))

    story.append(Paragraph(
        "DEEP-SKY IMAGING  &#8594;  Calibrated aesthetic + photometric outputs",
        st["h_item"]))
    story.append(Paragraph(
        "When no priority science workflow applies, deep-sky imaging "
        "produces calibrated long-exposure stacks via Siril's scriptable "
        "pipeline. Pretty pictures are a by-product of the science pipeline, "
        "not the goal -- and that means every aesthetic image ATLAS produces "
        "has photometric and astrometric metadata that makes it usable for "
        "follow-up measurements should something interesting appear. The "
        "image of Andromeda you made for your wall is also a calibrated "
        "scientific dataset.",
        st["body_dense"]))

    story.append(Paragraph(
        "TARGET PRIORITY  -  In order from the operator's brief",
        st["h_item"]))
    story.append(Paragraph(
        "<b>A.</b> Asteroid &amp; comet astrometry &nbsp;&nbsp;&#8226;&nbsp;&nbsp; "
        "<b>B.</b> Variable star + exoplanet photometry &nbsp;&nbsp;&#8226;&nbsp;&nbsp; "
        "<b>C1.</b> Supernova / transient hunting &nbsp;&nbsp;&#8226;&nbsp;&nbsp; "
        "<b>C2.</b> Planetary imaging &nbsp;&nbsp;&#8226;&nbsp;&nbsp; "
        "<b>D.</b> Deep-sky aesthetic.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 6: Imaging Discipline ----
    story.append(Paragraph("Imaging Discipline", st["h_section"]))
    story.append(Paragraph(
        "Real measurements require real calibration. ATLAS treats calibration "
        "and focus quality as non-negotiable. They are not features; they are "
        "the price of admission to the scientific record.",
        st["lead"]))

    story.append(Paragraph("Calibration library", st["h_item"]))
    story.append(Paragraph(
        "ATLAS maintains a calibration library indexed by frame type, filter, "
        "exposure time, gain, offset, and sensor temperature. Master bias "
        "frames. Master darks at every operating temperature x exposure pair "
        "the system uses. Master flats per filter, per session -- dawn sky "
        "flats are part of the standard close-out routine. Every science "
        "frame is calibrated against masters that match its acquisition "
        "parameters; the FITS header records the calibration sources used. "
        "The Critic continuously checks calibration freshness against a "
        "configurable window (default seven days) and flags stale or "
        "temperature-drifted masters before the operator notices.",
        st["body_dense"]))

    story.append(Paragraph("Autofocus, with policy per workflow", st["h_item"]))
    story.append(Paragraph(
        "Focus is not one-size-fits-all. ATLAS treats autofocus as a "
        "first-class workflow parameter, with policies tuned to the science:",
        st["body_dense"]))

    af_data = [
        ["Workflow", "Before seq.", "Filter chg.", "Temp delta", "Time int.", "HFR drift"],
        ["Astrometry",          "yes", "-",   "2 deg C",   "-",       "20 %"],
        ["Variable star photom.", "yes", "yes", "3 deg C",  "-",       "-"],
        ["Exoplanet transit",   "yes", "no",  "locked", "-",       "-"],
        ["Transient hunting",   "yes", "yes", "2 deg C",   "60 min",  "15 %"],
        ["Deep-sky imaging",    "yes", "yes", "2 deg C",   "60 min",  "15 %"],
        ["Planetary",           "yes", "no",  "locked", "-",       "-"],
    ]
    t = Table(af_data, colWidths=[1.5*inch, 0.85*inch, 0.9*inch, 1.0*inch, 0.85*inch, 0.85*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), NAVY),
        ("TEXTCOLOR",  (0,0), (-1,0), white),
        ("FONT", (0,0), (-1,0), "Helvetica-Bold", 8.5),
        ("FONT", (0,1), (-1,-1), "Helvetica", 8.5),
        ("ALIGN", (1,1), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("RIGHTPADDING", (0,0), (-1,-1), 5),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LINEBELOW", (0,0), (-1,-2), 0.3, RULE),
        ("BOX", (0,0), (-1,-1), 0.6, SOFT),
        ("BACKGROUND", (0,1), (-1,-1), LIGHT_BG),
    ]))
    story.append(t)
    story.append(Paragraph(
        "Exoplanet transits lock focus end-to-end so that no mid-transit "
        "focus shift contaminates the photometric baseline. Transient and "
        "deep-sky imaging refocus on temperature drift, time, or HFR drift, "
        "whichever fires first. The ZWO EAF (or any NINA-compatible focuser) "
        "is the executor; ATLAS is the brain.",
        st["body_dense"]))

    story.append(Paragraph("Plate solving and FITS metadata", st["h_item"]))
    story.append(Paragraph(
        "Every science frame is plate-solved with ASTAP (fast, offline-"
        "capable). The WCS solution is embedded in the FITS header of every "
        "archive frame. The header is populated with DATE-OBS to sub-second "
        "UTC, EXPTIME, FILTER, OBJECT, RA, DEC, AIRMASS, TELESCOP, INSTRUME, "
        "GAIN, EGAIN, CCD-TEMP, FOCAL_LEN, PIXSCALE, SITELAT, SITELONG, "
        "BAYERPAT, GUIDRMS, FWHM, and QUALITY. Anyone who later loads the "
        "frame -- including you, ten years from now -- has every piece of "
        "metadata needed to reproduce the measurement.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 7: The Database — Where Detection Happens ----
    story.append(Paragraph("The Database  -  Where Detection Happens", st["h_section"]))
    story.append(Paragraph(
        "Most observatory software writes a log. ATLAS builds a corpus. The "
        "database is not a record of what happened -- it is the instrument "
        "the Oracle uses to <i>find</i> what's happening. Every frame, every "
        "measurement, every decision, every alert, every inter-agent message "
        "is stored in queryable form. Years of nights become a single dataset "
        "you can ask questions of.",
        st["lead"]))

    story.append(Paragraph("Every detail, indexed", st["h_item"]))
    story.append(Paragraph(
        "Twenty tables, every one of them earning its place. Each captured "
        "frame is stored with its full FITS metadata, plate-solve status, "
        "WCS solution, FWHM, quality grade, calibration sources, gain, "
        "offset, temperature, filter, and a foreign key to its session and "
        "target. Each measurement -- photometric or astrometric -- carries "
        "its epoch UTC to sub-second precision, its value with uncertainty, "
        "the comp stars used, and the catalog reference for the cross-match. "
        "Each submission records its destination, status, formatted payload, "
        "operator approval, and external response. Reference frames, "
        "calibration masters, stack products, campaigns, knowledge threads, "
        "decisions, alerts, agent messages, storage events -- all indexed, "
        "all queryable, all auditable.",
        st["body_dense"]))

    story.append(Paragraph("What the Oracle does with it", st["h_item"]))
    story.append(Paragraph(
        "The Oracle's job is to look across this corpus and find what "
        "the operator should see. Six classes of query run continuously:",
        st["body_dense"]))
    oracle_queries = [
        ("Anomaly detection",
         "Photometric measurements that deviate from the historical baseline "
         "for a target. An unusual brightening at magnitude 14.2 when the "
         "comp-relative history says 14.7. Flagged to the Operator with the "
         "data trail."),
        ("Long-term trends",
         "Light curves assembled across every measurement of a target. "
         "Betelgeuse's slow decline across hundreds of nights. A periodic "
         "variable's amplitude shift across cycles. Trend fits with proper "
         "error propagation."),
        ("Cross-target correlations",
         "Quality drops correlated across unrelated targets in the same "
         "session usually mean an instrument problem (focus, dew, optics), "
         "not real physics. The Oracle distinguishes the two so the "
         "Archivist's hindsight verdict is calibrated."),
        ("Catalog cross-matching",
         "Every transient candidate is cross-matched against Gaia DR3, "
         "Pan-STARRS, the MPC, and recent TNS reports before it ever "
         "reaches the submission queue. False positives die here, not in "
         "the public record."),
        ("Knowledge thread maturation",
         "Each thread transitions data-driven: dormant -> active -> mature "
         "only when the success criterion is documented and met. The "
         "database knows when a campaign is done."),
        ("Decision audit with hindsight",
         "Every major agent decision -- go/no-go, standby, target switch -- "
         "writes its inputs, outputs, and rationale. The Oracle later joins "
         "the decision to its outcome and computes a hindsight verdict. "
         "Was the threshold right? Did the call hold up? This is how "
         "the system improves; this is how the operator trusts it."),
    ]
    for name, desc in oracle_queries:
        story.append(Paragraph(f"<b>{name}.</b> {desc}",
            ParagraphStyle("oq", parent=st["body_dense"],
                            leftIndent=12, spaceAfter=4)))

    story.append(Paragraph("Built for permanence", st["h_item"]))
    story.append(Paragraph(
        "SQLite by default; PostgreSQL migration path ready. Credentials "
        "(Anthropic API key, AAVSO/MPC/TNS tokens, ntfy.sh topic) are "
        "encrypted at rest with AES-256-GCM via an Argon2id-derived key. "
        "Backups and reinstalls preserve everything. Your science survives "
        "the next computer, the next OS, and the next decade.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 8: Campaign-Based Science ----
    story.append(Paragraph("Campaign-Based Science", st["h_section"]))
    story.append(Paragraph(
        "Real amateur science is not single-night work. It is cadence. It "
        "is observing a transit every week for a quarter, monitoring "
        "Betelgeuse every clear night for a year, recovering an asteroid "
        "across the first three clear nights after solar conjunction. ATLAS "
        "treats the multi-night research effort -- the <i>campaign</i> -- as "
        "a first-class object.",
        st["lead"]))

    story.append(Paragraph("Anatomy of a campaign", st["h_item"]))
    story.append(Paragraph(
        "A campaign carries a name, a science workflow, one or more targets, "
        "a priority, a cadence specification, a success criterion, a deadline "
        "if any, and a scientific context note explaining why this work "
        "matters. The Planner draws from active campaigns when building each "
        "night's schedule, weighting them by priority, urgency, and weather "
        "fit. The operator approves campaign activation; the Oracle proposes "
        "new campaigns from the database and the research agenda; the "
        "Planner schedules; the Operator commands.",
        st["body_dense"]))

    story.append(Paragraph("Three campaign examples", st["h_item"]))
    examples = [
        ("Betelgeuse V-band photometry - 12-month monitoring",
         "Cadence: every clear night. Success: 100 photometric points "
         "spanning one full year. Submission: AAVSO. Knowledge thread: "
         "transition from <i>active</i> to <i>mature</i> once the cycle is "
         "characterised."),
        ("TIC 1234567 transit confirmation - 4 events over 6 weeks",
         "Cadence: scheduled to the predicted transit windows. Success: 3 of "
         "4 events captured with adequate baseline. Submission: NASA Exoplanet "
         "Watch. Locked focus, fixed field, no dither, sub-second timing."),
        ("Asteroid 2024 XY recovery - 3 nights after solar conjunction",
         "Cadence: first three clear nights of next visibility window. "
         "Success: astrometric positions submitted to extend the orbital "
         "arc. Submission: MPC 80-column format. Non-sidereal tracking, "
         "short exposures, Gaia DR3 reference."),
    ]
    for name, desc in examples:
        story.append(Paragraph(f"&bull; <b>{name}</b>", st["bullet"]))
        story.append(Paragraph(desc, ParagraphStyle("ex", parent=st["body_dense"],
            leftIndent=18, spaceAfter=6)))

    story.append(Paragraph("Knowledge threads", st["h_item"]))
    story.append(Paragraph(
        "Each target carries one or more knowledge threads -- one per kind "
        "of science the target can support. A galaxy might have separate "
        "threads for imaging, transient watch, and photometry. Threads move "
        "between four states: <i>dormant</i> (no work yet), <i>active</i> "
        "(in progress), <i>mature</i> (success criterion met, but more data "
        "improves SNR), and <i>future</i> (planned but not yet started). "
        "Each thread carries its current open question and the threshold "
        "that would unlock the next phase. Discovery follows a thread; "
        "depth of understanding follows the maturation across threads.",
        st["body_dense"]))

    story.append(Paragraph("Research agenda intake", st["h_item"]))
    story.append(Paragraph(
        "ATLAS reads external alerts from AAVSO, the Astronomer's Telegram "
        "(ATel), the MPC Near-Earth Object Confirmation Page, and NASA "
        "Exoplanet Watch. Time-critical items -- transit windows, asteroid "
        "recovery deadlines, variable-star outburst follow-up -- are "
        "evaluated by the Oracle, prioritised by the operator, and "
        "scheduled by the Planner. The Critic surfaces approaching "
        "deadlines if a campaign is at risk of missing its window.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 8: Safety, Resilience, Discipline ----
    story.append(Paragraph("Safety, Resilience, and Discipline", st["h_section"]))
    story.append(Paragraph(
        "Equipment that runs unattended at 2 AM fails badly. ATLAS treats "
        "safety as architecture, not as a feature checkbox.",
        st["lead"]))

    story.append(Paragraph("Pre-flight checklist", st["h_item"]))
    story.append(Paragraph(
        "Before the Operator commands the roof to open, a complete pre-"
        "flight runs. Each item must pass -- or the operator must explicitly "
        "override. Items include:",
        st["body_dense"]))
    pre_items = [
        "NINA reachable and responsive on the configured host:port",
        "PHD2 reachable on its JSON-RPC port",
        "Camera connected, cooling has reached setpoint within tolerance",
        "Focuser connected and at a non-extreme position (not pinned at min or max)",
        "Mount connected and at a known parked position",
        "Filter wheel connected (if equipped)",
        "Recent master darks exist matching tonight's exposure plan",
        "Recent master flats exist for every filter in tonight's plan",
        "Disk free space exceeds the configured threshold",
        "Weather GO for the next 60 minutes",
        "Internet up -- or safe-autonomous mode is armed",
        "Anthropic Claude API responding",
        "Calibration freshness within the configured window (default 7 days)",
        "Power source nominal -- not on battery near shutdown threshold",
    ]
    for it in pre_items:
        story.append(Paragraph(f"&bull; {it}", st["bullet"]))

    story.append(Paragraph("Standby and emergency shutdown", st["h_item"]))
    story.append(Paragraph(
        "Two standby modes. <b>Light standby</b> pauses imaging, holds the "
        "mount, keeps the camera cooled -- fast resume. <b>Full standby</b> "
        "ramps the camera back to ambient, powers down hardware, parks the "
        "mount, closes the roof, and waits for explicit operator approval "
        "to resume. <b>Emergency shutdown</b> fires on hard-limit breach: "
        "stop imaging, park (verify), close the roof, warm the camera, "
        "power down, save state, push a critical alert to the human.",
        st["body_dense"]))

    story.append(Paragraph("Power-source awareness", st["h_item"]))
    story.append(Paragraph(
        "ATLAS detects the active power source via the OS UPS interface and "
        "supports an off-grid solar / battery / utility / generator stack "
        "explicitly. Default graceful-shutdown trigger: 50% remaining battery "
        "or 5 minutes runtime, whichever fires first. Resume after shutdown "
        "requires explicit operator approval.",
        st["body_dense"]))

    story.append(Paragraph("Offline-safe operation", st["h_item"]))
    story.append(Paragraph(
        "Imaging, guiding, focusing, and plate solving are local and survive "
        "an internet drop. Catalog lookups fall back to local caches; "
        "submissions queue. If the Claude API is unreachable, agents enter "
        "safe-autonomous mode: hold the current target, hold the schedule, "
        "reject non-trivial decisions, surface the outage. The night keeps "
        "producing science.",
        st["body_dense"]))
    story.append(PageBreak())

    # ---- PAGE 9: Operator Stays in Charge ----
    story.append(Paragraph("The Operator Stays in Charge", st["h_section"]))
    story.append(Paragraph(
        "Five AI agents do the heavy lifting, but the human is the "
        "scientist on the project. ATLAS is built around that asymmetry.",
        st["lead"]))

    story.append(Paragraph("Every submission queues for human approval", st["h_item"]))
    story.append(Paragraph(
        "MPC astrometry, AAVSO photometry, TNS transient reports, NASA "
        "Exoplanet Watch light curves -- none of these ever leave the "
        "building autonomously. Every candidate measurement appears in the "
        "Science tab with its exact submission payload, supporting cutouts, "
        "catalog cross-match results, and residual analysis. Approve, "
        "reject, or hold for review. Your observer code. Your name. Your "
        "scientific record. Real-name attribution is non-negotiable.",
        st["body_dense"]))

    story.append(Paragraph("Take Control toggle", st["h_item"]))
    story.append(Paragraph(
        "A persistent button at the top of every dashboard tab. While "
        "engaged, the agents pause command authority and continue only as "
        "observers and recorders. Direct hardware controls appear on the "
        "Tonight tab -- slew, capture, focus, filter wheel. Every action you "
        "take is logged into the session record with timestamp and "
        "rationale. Release control and the Operator returns to full "
        "authority, with the option to request a plan rebuild from the "
        "Planner if conditions have changed.",
        st["body_dense"]))

    story.append(Paragraph("Talk to ATLAS &#8212; text and voice", st["h_item"]))
    story.append(Paragraph(
        "The dashboard's ATLAS tab is a live conversation with the Operator "
        "agent. Type, or tap the microphone and speak: the browser's Web "
        "Speech API streams your voice to speech-to-text, ATLAS thinks, and "
        "the answer comes back as both text and spoken audio. Ask for "
        "tonight's GO/NO-GO, the current guiding RMS, why a target was "
        "skipped, or to start a sequence -- all hands-free from the warm "
        "room. No extra software is required on the warm-room PC; the "
        "feature works in any modern Chromium browser on the LAN.",
        st["body_dense"]))

    story.append(Paragraph("Decision audit trail", st["h_item"]))
    story.append(Paragraph(
        "Every major agent decision -- go/no-go, standby entry, target "
        "selection, schedule revision, calibration refresh, emergency "
        "shutdown -- writes a row to the decisions table with its inputs, "
        "outputs, rationale, and outcome. The session report's Decision "
        "Audit section presents each decision in order with a hindsight "
        "verdict: was the threshold right? Did the outcome justify the call? "
        "This is how the system learns; this is also how you, the operator, "
        "trust it.",
        st["body_dense"]))

    story.append(Paragraph("The 10-section session report", st["h_item"]))
    story.append(Paragraph(
        "Every session ends with an HTML report covering: executive summary, "
        "session timeline (imaging / standby / resume), per-target results "
        "with frames and integration totals, plan versions (v1 original, "
        "v2+ after standby or revision), equipment performance, processing "
        "recap with calibration sources and stack method, error log with "
        "severity tags, decision audit, campaign status with running totals, "
        "and recommendations for the next session. Self-documenting. "
        "Reproducible. Ready to share.",
        st["body_dense"]))

    story.append(Paragraph(
        "&#8220;The human approves. The system executes. Both are accountable.&#8221;",
        st["pull"]))
    story.append(PageBreak())

    # ---- PAGE 10: Closing ----
    story.append(Spacer(1, 0.9*inch))
    story.append(Paragraph(
        "For the backyard scientist who",
        ParagraphStyle("close1", parent=st["closing"], fontSize=15,
                        textColor=GREY_TEXT)))
    story.append(Paragraph(
        "takes this work seriously.",
        ParagraphStyle("close2", parent=st["closing"], fontSize=20,
                        fontName="Helvetica-Bold", textColor=NAVY, spaceAfter=24)))

    story.append(Paragraph(
        "You spent years choosing the mount, the optics, the camera. You "
        "learned collimation. You built a calibration library, refined "
        "polar alignment, weathered failed adapters and dead components. "
        "You did all of that because you wanted your nights to mean "
        "something. ATLAS is the software that says: yes, they do mean "
        "something -- let's make sure the rest of the scientific community "
        "knows it.",
        ParagraphStyle("close3", parent=st["body"], alignment=TA_CENTER,
                         leading=18, fontSize=12, leftIndent=30, rightIndent=30,
                         spaceAfter=16)))

    story.append(Paragraph(
        "Every measurement that leaves this observatory carries your "
        "observer code. Every supernova candidate you submit carries your "
        "name. Every asteroid astrometric position you produce extends "
        "an orbital arc that professional surveys will use to plan their "
        "next decade. The pier in your yard is a node in a global network "
        "of dedicated observers -- and ATLAS treats it accordingly.",
        ParagraphStyle("close4", parent=st["body"], alignment=TA_CENTER,
                         leading=18, fontSize=12, leftIndent=30, rightIndent=30,
                         spaceAfter=20)))

    story.append(Paragraph(
        "&#8212;",
        ParagraphStyle("dash", parent=st["closing"], fontSize=24,
                        textColor=GOLD, alignment=TA_CENTER)))

    story.append(Paragraph(
        "ATLAS.",
        ParagraphStyle("brand", parent=st["closing"],
                        fontSize=36, fontName="Helvetica-Bold",
                        textColor=NAVY, alignment=TA_CENTER, spaceBefore=14,
                        spaceAfter=10)))
    story.append(Paragraph(
        "Built for the amateur astronomer who wants their nights to count.",
        st["closing"]))

    story.append(Spacer(1, 0.4*inch))
    story.append(Paragraph(
        "Version: Design Phase 1.0  -  May 2026  -  MIT License  -  "
        "Distributable to amateur observatories worldwide.",
        ParagraphStyle("ver", parent=st["body"], alignment=TA_CENTER,
                         fontSize=9, textColor=SOFT)))

    def on_page(canv, doc_):
        if doc_.page == 1:
            return
        section_band(canv, doc_, "ATLAS  |  Autonomous Observatory Software")

    doc.build(story, onFirstPage=lambda c,d: None, onLaterPages=on_page)


# ============================================================================
# MANUAL — preserve existing
# ============================================================================

def make_manual(filename):
    """Keep the existing operator manual; only the brochure is being upgraded."""
    import os
    if os.path.exists(filename):
        return
    # Fallback stub if the manual ever goes missing
    doc = SimpleDocTemplate(filename, pagesize=LETTER)
    doc.build([Paragraph("ATLAS Operator Manual (placeholder)",
                          getSampleStyleSheet()["Title"])])


if __name__ == "__main__":
    import os
    out_dir = r"C:\ATLAS\docs"
    os.makedirs(out_dir, exist_ok=True)
    brochure_path = os.path.join(out_dir, "ATLAS_Brochure.pdf")
    manual_path = os.path.join(out_dir, "ATLAS_Operator_Manual.pdf")

    print("Building brochure (v2, 10 pages)...")
    make_brochure(brochure_path)
    size = os.path.getsize(brochure_path)
    print(f"  -> {brochure_path}  ({size/1024:.1f} KB)")

    print("Preserving existing operator manual...")
    make_manual(manual_path)
    print(f"  -> {manual_path}")

    print("Done.")
