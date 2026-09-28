import { inspectGreenhouse } from "./greenhouse";

// Content script is read-only in this phase. It exposes inspection only through
// message responses and never mutates the document.
chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object" || !("type" in message)) return false;
  const type = (message as { type?: unknown }).type;
  if (type === "INSPECT_FORM" || type === "GET_PAGE") {
    sendResponse({ ok: true, result: inspectGreenhouse() });
    return true;
  }
  if (type === "GET_CHALLENGE_STATE") {
    sendResponse({ ok: true, result: { state: "UNKNOWN" } });
    return true;
  }
  sendResponse({ ok: false, error: "command is not enabled in read-only phase" });
  return true;
});
