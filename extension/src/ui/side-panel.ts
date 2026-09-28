import { request } from "../protocol";

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (!element) throw new Error(`missing side-panel element: ${id}`);
  return element as T;
};

const status = byId<HTMLDivElement>("status");
const inspectButton = byId<HTMLButtonElement>("inspect");

function render(result: Record<string, unknown>): void {
  const fields = Array.isArray(result.fields) ? result.fields as Array<Record<string, unknown>> : [];
  byId("provider").textContent = String(result.provider ?? "—");
  byId("page-type").textContent = String(result.page_type ?? "—");
  byId("fields").textContent = String(fields.length);
  byId("resolved").textContent = "0";
  byId("missing").textContent = String(fields.filter((field) => field.required).length);
  byId("challenge").textContent = "unknown";
  const list = byId<HTMLUListElement>("field-list");
  list.replaceChildren(...fields.slice(0, 20).map((field) => {
    const item = document.createElement("li");
    item.textContent = `${String(field.label || field.id)}${field.required ? " *" : ""}`;
    return item;
  }));
}

inspectButton.addEventListener("click", () => {
  inspectButton.disabled = true;
  status.textContent = "Inspecting active tab…";
  chrome.runtime.sendMessage(request(crypto.randomUUID(), "INSPECT_FORM"), (reply: { ok?: boolean; result?: Record<string, unknown>; error?: string }) => {
    inspectButton.disabled = false;
    if (chrome.runtime.lastError || !reply?.ok) {
      status.textContent = reply?.error ?? chrome.runtime.lastError?.message ?? "Inspection failed";
      return;
    }
    render(reply.result ?? {});
    status.textContent = "Inspection complete. Fill and submit remain disabled.";
  });
});
