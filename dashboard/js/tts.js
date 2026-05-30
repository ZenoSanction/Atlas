// Shared TTS for the dashboard.
//
// Two problems the old per-file speak() functions had:
//
//   1. They sliced the text to 500-800 chars before speaking, silently
//      cutting off the rest of ATLAS's reply. Long answers (verdict
//      explanations, plan readouts) routinely overran that.
//
//   2. Chrome's speechSynthesis engine has a well-known bug: a single
//      utterance longer than ~200 chars / ~15s of audio gets killed
//      mid-stream — playback goes silent even though .speaking remains
//      true. The remaining text is dropped on the floor.
//
// The fix below is the standard browser workaround:
//
//   - Split the reply on sentence boundaries into chunks <= MAX_CHARS.
//   - Queue each chunk as its own utterance; chain via onend so they
//     play seamlessly.
//   - Run a keep-alive pump that calls pause()/resume() every ~10s
//     while we're speaking — this defeats the engine's idle timeout
//     even on chunks that approach the boundary.
//
// One place to silence everything: the localStorage key
// "atlas_tts_enabled" === "1" gates all speech. Same key the topbar
// 🔊 toggle in app.js writes to.

const MAX_CHARS    = 200;     // safely under Chrome's cutoff window
const KEEP_ALIVE_MS = 10_000; // pause+resume every 10s while speaking

let _queue = [];              // utterances pending after current
let _speaking = false;
let _keepAlive = null;

// ---- Public API ------------------------------------------------------

export function speak(text) {
  try {
    if (!("speechSynthesis" in window)) return;
    if (localStorage.getItem("atlas_tts_enabled") !== "1") return;
    const clean = (text || "").toString().trim();
    if (!clean) return;

    const chunks = chunkForTts(clean, MAX_CHARS);
    for (const c of chunks) {
      _queue.push(makeUtterance(c));
    }
    if (!_speaking) playNext();
  } catch (e) {
    console.warn("[tts] speak threw:", e);
  }
}

export function cancelSpeech() {
  _queue = [];
  stopKeepAlive();
  try { window.speechSynthesis.cancel(); } catch {}
  _speaking = false;
}

// ---- Internals -------------------------------------------------------

function makeUtterance(chunk) {
  const u = new SpeechSynthesisUtterance(chunk);
  u.lang  = "en-US";
  u.rate  = 1.05;
  u.pitch = 1.0;
  u.onend = onUtteranceEnd;
  u.onerror = (ev) => {
    console.warn("[tts] utterance error:", ev?.error || ev);
    onUtteranceEnd();   // keep the queue draining even on error
  };
  return u;
}

function playNext() {
  const next = _queue.shift();
  if (!next) {
    _speaking = false;
    stopKeepAlive();
    return;
  }
  _speaking = true;
  startKeepAlive();
  try {
    window.speechSynthesis.speak(next);
  } catch (e) {
    console.warn("[tts] speak threw:", e);
    onUtteranceEnd();
  }
}

function onUtteranceEnd() {
  // Slight tick so Chrome flushes the previous utterance state before
  // the next speak() — otherwise back-to-back utterances sometimes
  // collapse the second one's audio.
  setTimeout(playNext, 20);
}

// Chrome quirk: a long utterance gets paused by the engine's idle
// timeout. Calling pause() then resume() on a short timer resets the
// timeout and lets the audio keep flowing. Cheap; only runs while
// we're actually speaking.
function startKeepAlive() {
  if (_keepAlive) return;
  _keepAlive = setInterval(() => {
    try {
      if (window.speechSynthesis.speaking) {
        window.speechSynthesis.pause();
        window.speechSynthesis.resume();
      }
    } catch {}
  }, KEEP_ALIVE_MS);
}

function stopKeepAlive() {
  if (_keepAlive) {
    clearInterval(_keepAlive);
    _keepAlive = null;
  }
}

// Split into utterance-sized chunks on sentence boundaries.
//
// Implementation note: the obvious sentence-regex `[^.!?;:]+[.!?;:]?`
// silently DROPS chunks of text when the input contains decimal
// numbers ("6.2 degF" -> "2 degF") or 24-hour times ("03:00 EDT" ->
// "00 EDT") because the regex requires whitespace after the
// terminator and skips characters that don't match. Anything ATLAS
// says with a number in it would lose digits.
//
// We use a slice-based approach instead: find every TERMINATOR
// followed by whitespace, treat those as sentence breaks, and slice
// the original text between breaks. This preserves all characters.
// Then we greedily pack sentences into chunks <= maxChars, falling
// back to word-splitting only when one "sentence" is itself too
// long (rare, but possible with long campaign descriptions).
export function chunkForTts(text, maxChars = MAX_CHARS) {
  const out = [];
  const t = (text || "").toString().replace(/\s+/g, " ").trim();
  if (!t) return out;

  // Find every "terminator followed by whitespace" position. The
  // (?=\s) lookahead means we don't consume the whitespace, just
  // mark the break point right after the terminator. Decimal points
  // and colons inside times/IPs/MAC addresses are not followed by
  // whitespace, so they're correctly NOT treated as breaks.
  const breaks = [0];
  const re = /[.!?;:](?=\s)/g;
  let m;
  while ((m = re.exec(t)) !== null) breaks.push(m.index + 1);
  breaks.push(t.length);

  const sentences = [];
  for (let i = 0; i < breaks.length - 1; i++) {
    const s = t.slice(breaks[i], breaks[i + 1]).trim();
    if (s) sentences.push(s);
  }

  let buf = "";
  for (const piece of sentences) {
    if (piece.length > maxChars) {
      if (buf) { out.push(buf.trim()); buf = ""; }
      const words = piece.split(" ");
      let line = "";
      for (const w of words) {
        if ((line + " " + w).trim().length > maxChars) {
          if (line) out.push(line.trim());
          line = w;
        } else {
          line = line ? line + " " + w : w;
        }
      }
      if (line) out.push(line.trim());
      continue;
    }
    if ((buf + " " + piece).trim().length <= maxChars) {
      buf = buf ? buf + " " + piece : piece;
    } else {
      if (buf) out.push(buf.trim());
      buf = piece;
    }
  }
  if (buf) out.push(buf.trim());
  return out;
}
