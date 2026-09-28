import { request, type Command, type Request, type Response } from "./protocol";

const NATIVE_HOST_NAME = "com.job_agent_v2";
let nativePort: chrome.runtime.Port | undefined;
const usedSubmitTokens = new Set<string>();

function connectNative(): chrome.runtime.Port {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  nativePort.onDisconnect.addListener(() => {
    nativePort = undefined;
  });
  return nativePort;
}

function nativeRequest(message: Request): Promise<Response> {
  return new Promise((resolve, reject) => {
    let port: chrome.runtime.Port;
    try {
      port = connectNative();
    } catch (error) {
      reject(error);
      return;
    }
    let settled = false;
    const finish = (callback: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      callback();
    };
    const listener = (reply: unknown): void => {
      port.onMessage.removeListener(listener);
      finish(() => resolve(reply as Response));
    };
    const timeout = setTimeout(() => {
      port.onMessage.removeListener(listener);
      finish(() => reject(new Error("native host response timeout")));
    }, 5000);
    port.onMessage.addListener(listener);
    port.onDisconnect.addListener(() => {
      port.onMessage.removeListener(listener);
      const message = chrome.runtime.lastError?.message ?? "native host disconnected";
      finish(() => reject(new Error(message)));
    });
    port.postMessage(message);
  });
}

function response(requestId: string, result: unknown): Response {
  if (result && typeof result === "object" && "ok" in result) {
    const typed = result as { ok?: unknown; result?: unknown; error?: unknown };
    if (typed.ok === false) {
      return { version: 1, request_id: requestId, ok: false, error: String(typed.error ?? "content script rejected request") };
    }
    return {
      version: 1,
      request_id: requestId,
      ok: true,
      result: (typed.result && typeof typed.result === "object" ? typed.result : {}) as Record<string, unknown>,
    };
  }
  return { version: 1, request_id: requestId, ok: false, error: "invalid content response" };
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;
  const input = message as { request_id?: unknown; type?: unknown; payload?: unknown };
  const requestId = typeof input.request_id === "string" ? input.request_id : crypto.randomUUID();
  const type = input.type as Command;

  if (type === "PING") {
    void nativeRequest(request(requestId, "PING"))
      .then(sendResponse)
      .catch((error: unknown) => sendResponse({ version: 1, request_id: requestId, ok: false, error: String(error) }));
    return true;
  }
  if (type === "REQUEST_SUBMIT") {
    const payload = input.payload;
    const authorization = payload && typeof payload === "object"
      ? (payload as { authorization?: { token?: unknown } }).authorization
      : undefined;
    const token = authorization && typeof authorization.token === "string" ? authorization.token : "";
    if (!token || usedSubmitTokens.has(token)) {
      sendResponse({ version: 1, request_id: requestId, ok: false, error: "submit authorization was already used or is missing" });
      return true;
    }
    usedSubmitTokens.add(token);
  } else if (type !== "GET_PAGE" && type !== "INSPECT_FORM" && type !== "READ_FORM" && type !== "FILL_FORM" && type !== "UPLOAD_ARTIFACT" && type !== "GET_CHALLENGE_STATE" && type !== "GET_SUBMIT_RESULT") {
    sendResponse({ version: 1, request_id: requestId, ok: false, error: "command disabled in read-only phase" });
    return true;
  }

  void chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
    if (!tab?.id) {
      sendResponse({ version: 1, request_id: requestId, ok: false, error: "no active tab" });
      return;
    }
    void chrome.tabs.sendMessage(tab.id, { type, payload: input.payload ?? {} }).then(
      (result: unknown) => sendResponse(response(requestId, result)),
      (error: unknown) => sendResponse({ version: 1, request_id: requestId, ok: false, error: String(error) }),
    );
  });
  return true;
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);

// Establish the native host lazily on the first request. A missing host is
// reported by the PING response; the read-only inspection path remains usable.
