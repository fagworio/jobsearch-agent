import { request, type Command, type Request, type Response } from "./protocol";

const NATIVE_HOST_NAME = "com.job_agent_v2";
let nativePort: chrome.runtime.Port | undefined;
const usedSubmitTokens = new Set<string>();

type TabContext = {
  tab_id: number;
  provider: "greenhouse";
  job_id: string;
  url: string;
};

const tabContexts = new Map<number, TabContext>();

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  const context = tabContexts.get(tabId);
  if (context && changeInfo.url) {
    try {
      if (new URL(changeInfo.url).hostname !== "my.greenhouse.io") tabContexts.delete(tabId);
    } catch {
      tabContexts.delete(tabId);
    }
  }
});
chrome.tabs.onRemoved.addListener((tabId) => {
  tabContexts.delete(tabId);
});

function connectNative(): chrome.runtime.Port {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  nativePort.onDisconnect.addListener(() => {
    // Reading lastError prevents Chrome from reporting an unchecked error;
    // logging it keeps the failure diagnosable without surfacing a noisy
    // extension error page to the user.
    const error = chrome.runtime.lastError?.message;
    if (error) console.warn("[Job Agent] native host disconnected:", error);
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

function discoveryUrl(payload: unknown): string {
  if (!payload || typeof payload !== "object") throw new Error("payload must be an object");
  const input = payload as { query?: unknown; work_type?: unknown };
  if (typeof input.query !== "string" || !input.query.trim() || input.query.length > 255) {
    throw new Error("query must be a non-empty string of at most 255 characters");
  }
  const workType = input.work_type ?? ["remote"];
  if (!Array.isArray(workType) || !workType.length || !workType.every((value) => ["remote", "hybrid", "in_person"].includes(value))) {
    throw new Error("work_type must contain only remote, hybrid or in_person");
  }
  const url = new URL("https://my.greenhouse.io/jobs/search");
  url.searchParams.set("query", input.query.trim());
  for (const value of workType) url.searchParams.append("work_type[]", value);
  return url.href;
}

function requireObject(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("payload must be an object");
  return payload as Record<string, unknown>;
}

function greenhouseJobUrl(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) throw new Error("url must be a non-empty string");
  const url = new URL(value);
  if (url.protocol !== "https:" || url.hostname !== "my.greenhouse.io" || !/^\/jobs\//.test(url.pathname)) {
    throw new Error("url must be a MyGreenhouse job URL");
  }
  return url.href;
}

function contextPayload(context: TabContext): Record<string, unknown> {
  return { ...context };
}

function requireContext(payload: unknown): TabContext {
  const input = requireObject(payload);
  if (!Number.isInteger(input.tab_id)) throw new Error("tab_id is required");
  const context = tabContexts.get(input.tab_id as number);
  if (!context) throw new Error("tab context is not registered");
  if (input.provider !== undefined && input.provider !== context.provider) throw new Error("provider does not match tab context");
  if (input.job_id !== undefined && input.job_id !== context.job_id) throw new Error("job_id does not match tab context");
  return context;
}

function sameRoute(actual: string | undefined, expected: string, ignoreSearch: boolean): boolean {
  if (!actual) return false;
  if (!ignoreSearch) return actual === expected;
  try {
    const left = new URL(actual);
    const right = new URL(expected);
    return left.origin === right.origin && left.pathname === right.pathname;
  } catch {
    return false;
  }
}

async function waitForTab(tabId: number, expectedUrl: string, ignoreSearch = false): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (error?: unknown): void => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timeout);
      if (error) reject(error); else resolve();
    };
    const listener = (updatedTabId: number, changeInfo: chrome.tabs.TabChangeInfo, tab: chrome.tabs.Tab): void => {
      if (updatedTabId === tabId && changeInfo.status === "complete" && sameRoute(tab.url, expectedUrl, ignoreSearch)) finish();
    };
    const timeout = setTimeout(() => finish(new Error("tab navigation timed out")), 30_000);
    chrome.tabs.onUpdated.addListener(listener);
    void chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete" && sameRoute(tab.url, expectedUrl, ignoreSearch)) finish();
    }).catch(finish);
  });
}

async function discoverQuery(tabId: number, payload: unknown): Promise<Record<string, unknown>> {
  const url = discoveryUrl(payload);
  const current = await chrome.tabs.get(tabId);
  if (current.url !== url) await chrome.tabs.update(tabId, { url });
  await waitForTab(tabId, url);
  // MyGreenhouse completes the document navigation before its result cards
  // finish rendering. Give the read-only page state a short settling window.
  await new Promise((resolve) => setTimeout(resolve, 2_000));
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const reply = response(crypto.randomUUID(), await chrome.tabs.sendMessage(tabId, { type: "GET_DISCOVERY_RESULTS", payload: {} }));
      if (reply.ok) return reply.result ?? {};
      throw new Error(reply.error ?? "discovery result inspection failed");
    } catch (error) {
      if (attempt === 19) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw new Error("discovery result inspection failed");
}

async function openJob(payload: unknown): Promise<Record<string, unknown>> {
  const input = requireObject(payload);
  if (input.provider !== "greenhouse") throw new Error("only provider=greenhouse is enabled");
  if (typeof input.job_id !== "string" || !input.job_id.trim()) throw new Error("job_id must be a non-empty string");
  const url = greenhouseJobUrl(input.url);
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("no active tab");
  const context: TabContext = { tab_id: tab.id, provider: "greenhouse", job_id: input.job_id, url };
  tabContexts.set(tab.id, context);
  if (tab.url !== url) await chrome.tabs.update(tab.id, { url });
  await waitForTab(tab.id, url, true);
  const loaded = await chrome.tabs.get(tab.id);
  const bound = { ...context, url: loaded.url ?? url };
  tabContexts.set(tab.id, bound);
  return contextPayload(bound);
}

async function waitForApplication(tabId: number, context: TabContext): Promise<Record<string, unknown>> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const reply = response(
        crypto.randomUUID(),
        await chrome.tabs.sendMessage(tabId, { type: "WAIT_FOR_APPLICATION", payload: {} }),
      );
      if (reply.ok) return { ...contextPayload(context), ...(reply.result ?? {}) };
      throw new Error(reply.error ?? "application form was not found");
    } catch (error) {
      if (attempt === 59) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  throw new Error("application form wait timed out");
}

async function targetTab(type: Command, payload: unknown): Promise<chrome.tabs.Tab> {
  const input = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const contextBound = type === "GET_TAB_CONTEXT" || type === "WAIT_FOR_APPLICATION" || input.tab_id !== undefined;
  if (contextBound) {
    const context = requireContext(payload);
    const tab = await chrome.tabs.get(context.tab_id);
    if (!tab.id) throw new Error("context tab is unavailable");
    return tab;
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("no active tab");
  return tab;
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
    const metadata = authorization as { tab_id?: unknown; provider?: unknown; canonical_job_id?: unknown } | undefined;
    if (metadata?.tab_id !== undefined) {
      try {
        const context = requireContext(payload);
        if (metadata.tab_id !== context.tab_id) throw new Error("authorization tab_id does not match request context");
        if (metadata.provider !== undefined && metadata.provider !== context.provider) throw new Error("authorization provider does not match request context");
        if (metadata.canonical_job_id !== undefined && metadata.canonical_job_id !== context.job_id) throw new Error("authorization job_id does not match request context");
      } catch (error) {
        return { version: 1, request_id: requestId, ok: false, error: String(error) };
      }
    }
    const token = authorization && typeof authorization.token === "string" ? authorization.token : "";
    if (!token || usedSubmitTokens.has(token)) {
      return { version: 1, request_id: requestId, ok: false, error: "submit authorization was already used or is missing" };
    }
  } else if (type !== "GET_AUTH_STATE" && type !== "GET_PAGE" && type !== "GET_DISCOVERY_RESULTS" && type !== "GET_DISCOVERY_FILTERS" && type !== "DISCOVER_QUERY" && type !== "OPEN_JOB" && type !== "GET_TAB_CONTEXT" && type !== "WAIT_FOR_APPLICATION" && type !== "REMOVE_REPEATABLE_ENTRY" && type !== "INSPECT_FORM" && type !== "GET_FIELD_OPTIONS" && type !== "READ_FORM" && type !== "FILL_FORM" && type !== "UPLOAD_ARTIFACT" && type !== "GET_CHALLENGE_STATE" && type !== "GET_SUBMIT_RESULT") {
    return { version: 1, request_id: requestId, ok: false, error: "command disabled in read-only phase" };
  }

  try {
    if (type === "OPEN_JOB") {
      return { version: 1, request_id: requestId, ok: true, result: await openJob(input.payload ?? {}) };
    }
    if (type === "GET_TAB_CONTEXT") {
      return { version: 1, request_id: requestId, ok: true, result: contextPayload(requireContext(input.payload ?? {})) };
    }
    const tab = await targetTab(type, input.payload ?? {});
    if (!tab.id) throw new Error("target tab is unavailable");
    if (type === "WAIT_FOR_APPLICATION") {
      return { version: 1, request_id: requestId, ok: true, result: await waitForApplication(tab.id, requireContext(input.payload ?? {})) };
    }
    if (type === "DISCOVER_QUERY") {
      return { version: 1, request_id: requestId, ok: true, result: await discoverQuery(tab.id, input.payload ?? {}) };
    }
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
      console.info("[Job Agent] Greenhouse content script is ready");
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
