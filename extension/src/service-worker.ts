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
  nativePort.onMessage.addListener((message: unknown) => {
    if (!message || typeof message !== "object" || !("type" in message)) return;
    const port = nativePort;
    if (!port) return;
    void handleRequest(message as Request).then((reply) => port.postMessage(reply));
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

async function handleRequest(message: unknown): Promise<Response> {
  if (!message || typeof message !== "object") {
    return { version: 1, request_id: crypto.randomUUID(), ok: false, error: "message must be an object" };
  }
  const input = message as { request_id?: unknown; type?: unknown; payload?: unknown };
  const requestId = typeof input.request_id === "string" ? input.request_id : crypto.randomUUID();
  const type = input.type as Command;

  if (type === "HELLO") {
    return { version: 1, request_id: requestId, ok: true, result: { name: "job-agent-v2", protocol_version: 1 } };
  }
  if (type === "PING") {
    return { version: 1, request_id: requestId, ok: true, result: { type: "PONG" } };
  }
  if (type === "REQUEST_SUBMIT") {
    const payload = input.payload;
    const authorization = payload && typeof payload === "object"
      ? (payload as { authorization?: { token?: unknown } }).authorization
      : undefined;
    const token = authorization && typeof authorization.token === "string" ? authorization.token : "";
    if (!token || usedSubmitTokens.has(token)) {
      return { version: 1, request_id: requestId, ok: false, error: "submit authorization was already used or is missing" };
    }
  } else if (type !== "GET_PAGE" && type !== "INSPECT_FORM" && type !== "READ_FORM" && type !== "FILL_FORM" && type !== "UPLOAD_ARTIFACT" && type !== "GET_CHALLENGE_STATE" && type !== "GET_SUBMIT_RESULT") {
    return { version: 1, request_id: requestId, ok: false, error: "command disabled in read-only phase" };
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return { version: 1, request_id: requestId, ok: false, error: "no active tab" };
  try {
    const reply = response(
      requestId,
      await chrome.tabs.sendMessage(tab.id, { type, payload: input.payload ?? {} }),
    );
    if (type === "REQUEST_SUBMIT" && reply.ok) {
      const payload = input.payload;
      const authorization = payload && typeof payload === "object"
        ? (payload as { authorization?: { token?: unknown } }).authorization
        : undefined;
      if (authorization && typeof authorization.token === "string") usedSubmitTokens.add(authorization.token);
    }
    return reply;
  } catch (error) {
    return { version: 1, request_id: requestId, ok: false, error: String(error) };
  }
}

chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (message && typeof message === "object" && (message as { type?: unknown }).type === "EXTENSION_READY") {
    try {
      connectNative();
      sendResponse({ ok: true });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
    return true;
  }
  if (message && typeof message === "object" && (message as { type?: unknown }).type === "PING") {
    void nativeRequest(request(crypto.randomUUID(), "PING"))
      .then(sendResponse)
      .catch((error: unknown) => sendResponse({ version: 1, request_id: crypto.randomUUID(), ok: false, error: String(error) }));
    return true;
  }
  void handleRequest(message).then(sendResponse);
  return true;
});

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => undefined);

// The backend reaches this already-running host through its private local
// socket. The browser session remains owned by Chrome.
try {
  connectNative();
} catch {
  // A missing host is reported when the backend or side panel requests it.
}
