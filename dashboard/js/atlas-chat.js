// ATLAS chat — text + browser Web Speech voice.
//
// The voice path has three failure modes the operator can't see otherwise:
//
//   1. The mic button is wired as a click-toggle (NOT press-and-hold).
//      Click once to start, click again to stop. The tooltip used to
//      say "Hold to speak" which led to the operator literally holding
//      the button down and seeing nothing happen.
//
//   2. Chrome's SpeechRecognition uses a network round-trip to Google.
//      Offline / blocked mic / wrong device all surface through
//      `recog.onerror`. We now show that error in the chat history so
//      the operator can tell exactly what went wrong (permission
//      denied vs no audio capture vs network vs aborted).
//
//   3. TTS playback (the "ATLAS speaks the reply" bit) honours the 🔊
//      toggle in the topbar — same key as the per-lane chats — so the
//      user has ONE place to silence the dashboard.

export function initChat(api) {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const history = document.getElementById("chat-history");
  const voiceBtn = document.getElementById("voice-btn");

  // Text submit -------------------------------------------------------------
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    appendMsg(history, "user", "You", text);
    input.value = "";
    appendMsg(history, "atlas", "ATLAS", "(thinking…)");
    try {
      const r = await api("/atlas/chat", {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      history.lastElementChild.remove();
      appendMsg(history, "atlas", "ATLAS",
                r.reply + (r.safe_mode ? "\n\n(safe-autonomous mode)" : ""));
      speak(r.reply);
    } catch (e) {
      history.lastElementChild.remove();
      appendMsg(history, "atlas", "ATLAS", `Error: ${e.message}`);
    }
  });

  // Voice input — Web Speech API -------------------------------------------
  const Recog = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recog) {
    voiceBtn.disabled = true;
    voiceBtn.title =
      "Voice input not supported by this browser. Use Chrome or Edge.";
    voiceBtn.style.opacity = "0.4";
    return;
  }

  // Single long-lived recognizer. Chrome's SpeechRecognition raises
  // InvalidStateError if you call start() while a previous session is
  // still active, so we guard with our own `isRecording` flag and
  // abort() defensively before re-starting.
  const recog = new Recog();
  recog.continuous = false;
  recog.interimResults = false;
  recog.lang = "en-US";

  let isRecording = false;
  const setRecording = (on) => {
    isRecording = on;
    voiceBtn.classList.toggle("recording", on);
    voiceBtn.title = on
      ? "Listening… click to stop."
      : "Click to speak. Click again to stop.";
  };
  // Replace the old misleading "Hold to speak" tooltip on load.
  setRecording(false);

  voiceBtn.addEventListener("click", () => {
    if (isRecording) {
      try { recog.stop(); } catch {}
      setRecording(false);
      return;
    }
    try {
      // Defensive: abort any stale session before starting fresh.
      try { recog.abort(); } catch {}
      recog.start();
      // setRecording(true) deferred to onstart so the button lights up
      // only once the mic actually opens. If start() fails, we won't
      // get a misleading "recording" state stuck on screen.
    } catch (err) {
      setRecording(false);
      appendMsg(history, "atlas", "ATLAS",
                `Voice input couldn't start: ${err?.message || err}. ` +
                `Try clicking the mic icon in Chrome's address bar to ` +
                `check permissions.`);
    }
  });

  recog.onstart = () => setRecording(true);
  recog.onaudiostart = () => {
    // No-op for now, but here so we can wire a "listening" indicator
    // when we add a waveform later.
  };
  recog.onresult = (ev) => {
    const transcript = ev.results[0][0].transcript;
    input.value = transcript;
    setRecording(false);
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  };
  recog.onend = () => setRecording(false);

  // Loud failure messages — the actual fix for the silent-failure bug.
  recog.onerror = (ev) => {
    setRecording(false);
    const code = ev?.error || "unknown";
    const msg = errorMessage(code);
    appendMsg(history, "atlas", "ATLAS",
              `Voice input failed (${code}): ${msg}`);
    // Also log to the console for the dev tools view, since the chat
    // pane can be small.
    console.warn("[atlas-chat] SpeechRecognition error:", code, ev);
  };
}

function errorMessage(code) {
  switch (code) {
    case "not-allowed":
    case "service-not-allowed":
      return "microphone permission denied. Click the 🎙 icon in " +
             "Chrome's address bar (or chrome://settings/content/microphone) " +
             "and allow this page.";
    case "no-speech":
      return "no speech detected — try speaking right after clicking the " +
             "button, and check that the right input device is selected.";
    case "audio-capture":
      return "no microphone found. Check that your headset/mic is plugged " +
             "in and Windows has the right default device.";
    case "network":
      return "network error. Chrome's speech recognition needs internet " +
             "access (it streams audio to Google).";
    case "aborted":
      return "recording was aborted.";
    case "language-not-supported":
      return "the recognizer language isn't supported in this browser.";
    default:
      return "see the browser console for details.";
  }
}

function appendMsg(history, cls, who, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.innerHTML = `<span class="who">${who}:</span><span>${esc(text)}</span>`;
  history.appendChild(div);
  history.scrollTop = history.scrollHeight;
}

// Browser TTS. Honours the 🔊 topbar toggle (same localStorage key the
// per-lane chats use), so there's one place to silence the dashboard.
// Failures used to be silent — we surface onerror in the console now so
// "I clicked send and nothing spoke" can actually be diagnosed.
function speak(text) {
  try {
    if (!("speechSynthesis" in window)) return;
    if (localStorage.getItem("atlas_tts_enabled") !== "1") return;
    if (!text) return;
    const u = new SpeechSynthesisUtterance(String(text).slice(0, 800));
    u.lang = "en-US";
    u.rate = 1.05;
    u.onerror = (ev) =>
      console.warn("[atlas-chat] TTS error:", ev?.error || ev);
    window.speechSynthesis.speak(u);
  } catch (e) {
    console.warn("[atlas-chat] TTS threw:", e);
  }
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;")
                       .replace(/</g, "&lt;")
                       .replace(/>/g, "&gt;");
}
