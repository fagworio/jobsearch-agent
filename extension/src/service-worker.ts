import { request, type Command, type Response } from "./protocol";

function response(requestId: string, result: Record<string, unknown>): Response {
  return { version: 1, request_id: requestId, ok: true, result };
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  const input = message as { request_id?: unknown; type?: unknown; payload?: unknown };
  const requestId = typeof input.request_id === "string" ? input.request_id : crypto.randomUUID();
  const type = input.type as Command;

  if (type === "PING") {
    sendResponse(response(requestId, { type: "PONG" }));
    return true;
  }
  if (type !== "GET_PAGE" && type !== "INSPECT_FORM" && type !== "GET_CHALLENGE_STATE") {
    sendResponse({ version: 1, request_id: requestId, ok: false, error: "command disabled in read-only phase" });
    return true;
  }

  void chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
    if (!tab?.id) {
      sendResponse({ version: 1, request_id: requestId, ok: false, error: "no active tab" });
      return;
    }
    void chrome.tabs.sendMessage(tab.id, { type, payload: input.payload ?? {} }).then(
      (result: unknown) => sendResponse(response(requestId, (result as { result?: Record<string, unknown> }).result ?? {})),
      (error: unknown) => sendResponse({ version: 1, request_id: requestId, ok: false, error: String(error) }),
    );
  });
  return true;
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);

// Keep the protocol import exercised by the service worker build and make the
// permitted command surface explicit at the extension boundary.
void request("startup", "HELLO");
