import { request, type Command, type Request, type Response } from "./protocol";

const NATIVE_HOST_NAME = "com.job_agent_v2";
let nativePort: chrome.runtime.Port | undefined;

function connectNative(): chrome.runtime.Port {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  nativePort.onDisconnect.addListener(() => {
    nativePort = undefined;
  });
  return nativePort;
}

function nativeRequest(message: Request): Promise<Response> {
  return new Promise((resolve) => {
    const port = connectNative();
    const listener = (reply: unknown): void => {
      port.onMessage.removeListener(listener);
      resolve(reply as Response);
    };
    port.onMessage.addListener(listener);
    port.postMessage(message);
  });
}

function response(requestId: string, result: Record<string, unknown>): Response {
  return { version: 1, request_id: requestId, ok: true, result };
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  const input = message as { request_id?: unknown; type?: unknown; payload?: unknown };
  const requestId = typeof input.request_id === "string" ? input.request_id : crypto.randomUUID();
  const type = input.type as Command;

  if (type === "PING") {
    void nativeRequest(request(requestId, "PING")).then(sendResponse);
    return true;
  }
  if (type !== "GET_PAGE" && type !== "INSPECT_FORM" && type !== "READ_FORM" && type !== "FILL_FORM" && type !== "GET_CHALLENGE_STATE") {
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

// Establish the native host lazily on the first request. A missing host is
// reported by the PING response; the read-only inspection path remains usable.
