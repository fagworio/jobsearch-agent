import { request } from "../protocol";

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing side-panel element: ${id}`);
  return element as T;
};

const status = byId<HTMLDivElement>("status");
const inspectButton = byId<HTMLButtonElement>("inspect");
const authButton = byId<HTMLButtonElement>("check-login");

function render(result: Record<string, unknown>, challenge: Record<string, unknown>): void {
  const fields = Array.isArray(result.fields) ? result.fields as Array<Record<string, unknown>> : [];
  byId("provider").textContent = String(result.provider ?? "—");
  byId("page-type").textContent = String(result.page_type ?? "—");
  byId("surface").textContent = String(result.surface ?? "document");
  byId("fields").textContent = String(fields.length);
  const unresolved = (field: Record<string, unknown>): boolean => {
    const value = String(field.value ?? "").trim();
    return !value || /^select(?:\.\.\.)?$/i.test(value);
  };
  byId("resolved").textContent = String(fields.filter((field) => !unresolved(field)).length);
  byId("missing").textContent = String(fields.filter((field) => field.required && unresolved(field)).length);
  byId("challenge").textContent = String(challenge.state ?? "UNKNOWN");
  const list = byId<HTMLUListElement>("field-list");
  list.replaceChildren(...fields.slice(0, 20).map((field) => {
    const item = document.createElement("li");
    item.textContent = `${String(field.label || field.id)}${field.required ? " *" : ""}`;
    return item;
  }));
}

function renderAuth(result: Record<string, unknown>): void {
  byId("auth-status").textContent = String(result.state ?? "UNKNOWN");
  byId("auth-detail").textContent = String(result.detail ?? "");
}

function checkAuth(): void {
  authButton.disabled = true;
  chrome.runtime.sendMessage(request(crypto.randomUUID(), "GET_AUTH_STATE"), (reply: { ok?: boolean; result?: Record<string, unknown>; error?: string }) => {
    authButton.disabled = false;
    if (chrome.runtime.lastError || !reply?.ok) {
      byId("auth-status").textContent = "UNAVAILABLE";
      byId("auth-detail").textContent = reply?.error ?? chrome.runtime.lastError?.message ?? "Login status unavailable";
      return;
    }
    renderAuth(reply.result ?? {});
  });
}

authButton.addEventListener("click", checkAuth);
byId<HTMLButtonElement>("open-login").addEventListener("click", () => {
  void chrome.tabs.create({ url: "https://my.greenhouse.io/users/sign_in" });
});

inspectButton.addEventListener("click", () => {
  inspectButton.disabled = true;
  status.textContent = "Inspecting active tab…";
  chrome.runtime.sendMessage(request(crypto.randomUUID(), "INSPECT_FORM"), (reply: { ok?: boolean; result?: Record<string, unknown>; error?: string }) => {
    inspectButton.disabled = false;
    if (chrome.runtime.lastError || !reply?.ok) {
      status.textContent = reply?.error ?? chrome.runtime.lastError?.message ?? "Inspection failed";
      return;
    }
    chrome.runtime.sendMessage(request(crypto.randomUUID(), "GET_CHALLENGE_STATE"), (challengeReply: { ok?: boolean; result?: Record<string, unknown>; error?: string }) => {
      render(reply.result ?? {}, challengeReply?.ok ? challengeReply.result ?? {} : { state: "UNKNOWN" });
      status.textContent = "Inspection complete. Fill and submit remain disabled.";
    });
  });
});

checkAuth();
