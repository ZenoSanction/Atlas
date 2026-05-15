// ATLAS chat — text + browser Web Speech voice.

export function initChat(api) {
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const history = document.getElementById("chat-history");
  const voiceBtn = document.getElementById("voice-btn");

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
      // Replace the "thinking" placeholder
      history.lastElementChild.remove();
      appendMsg(history, "atlas", "ATLAS", r.reply + (r.safe_mode ? "\n\n(safe-autonomous mode)" : ""));
      speak(r.reply);
    } catch (e) {
      history.lastElementChild.remove();
      appendMsg(history, "atlas", "ATLAS", `Error: ${e.message}`);
    }
  });

  // Voice input — Web Speech API
  if ("webkitSpeechRecognition" in window || "SpeechRecognition" in window) {
    const Recog = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recog = new Recog();
    recog.continuous = false;
    recog.interimResults = false;
    recog.lang = "en-US";

    voiceBtn.addEventListener("click", () => {
      if (voiceBtn.classList.contains("recording")) {
        recog.stop();
      } else {
        recog.start();
        voiceBtn.classList.add("recording");
      }
    });
    recog.onresult = (e) => {
      const t = e.results[0][0].transcript;
      input.value = t;
      form.dispatchEvent(new Event("submit"));
    };
    recog.onend = () => voiceBtn.classList.remove("recording");
    recog.onerror = () => voiceBtn.classList.remove("recording");
  } else {
    voiceBtn.disabled = true;
    voiceBtn.title = "Voice input not supported by this browser.";
    voiceBtn.style.opacity = "0.4";
  }
}

function appendMsg(history, cls, who, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.innerHTML = `<span class="who">${who}:</span><span>${esc(text)}</span>`;
  history.appendChild(div);
  history.scrollTop = history.scrollHeight;
}

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  u.rate = 1.05;
  window.speechSynthesis.speak(u);
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
