// Voice-input support detection — shared by atlas-chat.js + mission-control.js.
//
// Chrome's SpeechRecognition API has TWO hard requirements that bite when
// the operator opens the dashboard from the warm-room PC over the LAN:
//
//   1. Secure context. localhost is treated as secure even on HTTP, but
//      a LAN URL like http://atlas-pc:8080 is NOT a secure context.
//      Chrome will silently block mic access — the recog.start() call
//      either fails with "not-allowed" or never opens the mic at all.
//      Switching to https:// (ATLAS has TLS support via `atlas serve
//      --https`) lifts the block.
//
//   2. Internet reachability. Chrome streams recorded audio to Google's
//      speech servers; an offline observatory can't use voice input
//      even on a secure context.
//
// This module's job: detect (1) at boot, BEFORE the user clicks the mic
// button, and return a structured verdict so the caller can disable the
// button with a tooltip + post a one-time chat banner explaining the
// fix. Failing loudly up front beats failing silently after a click.

/**
 * Inspect the browser context and return:
 *   {
 *     supported: bool,              // SpeechRecognition exists at all
 *     secureContext: bool,          // window.isSecureContext
 *     usable: bool,                 // supported && secureContext
 *     reason: "ok" | "no_api" | "insecure_origin",
 *     hint: string                  // human-readable explanation
 *   }
 */
export function checkVoiceInputSupport() {
  const supported = !!(window.SpeechRecognition
                          || window.webkitSpeechRecognition);
  // isSecureContext is true on https://, http://localhost, and file://.
  // It's false on http://192.168.x.x, http://atlas-pc:8080, etc.
  const secureContext = !!window.isSecureContext;

  if (!supported) {
    return {
      supported: false,
      secureContext,
      usable: false,
      reason: "no_api",
      hint: ("Voice input is not supported by this browser. "
             + "Use Chrome or Edge."),
    };
  }
  if (!secureContext) {
    const here = location.host || "this address";
    return {
      supported: true,
      secureContext: false,
      usable: false,
      reason: "insecure_origin",
      hint: ("Voice input is blocked by Chrome on plain HTTP from a "
             + `non-localhost address (you are on http://${here}). `
             + "Open the dashboard over HTTPS instead — restart ATLAS "
             + "with `atlas serve --https` and reload using the "
             + "`https://…` URL. You may need to accept the self-signed "
             + "certificate the first time."),
    };
  }
  return {
    supported: true,
    secureContext: true,
    usable: true,
    reason: "ok",
    hint: "",
  };
}

/**
 * Apply the verdict to a mic button: disable + tooltip when unusable.
 * Returns the verdict for the caller's own logic.
 */
export function applyVoiceSupportToButton(button) {
  const v = checkVoiceInputSupport();
  if (!button) return v;
  if (!v.usable) {
    button.disabled = true;
    button.title = v.hint;
    button.style.opacity = "0.4";
    button.setAttribute("data-voice-blocked", v.reason);
  } else {
    button.disabled = false;
    button.removeAttribute("data-voice-blocked");
  }
  return v;
}
