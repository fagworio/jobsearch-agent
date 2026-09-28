import { inspectGreenhouse } from "./greenhouse";

type FillPayload = {
  field_id?: unknown;
  action?: unknown;
  value?: unknown;
};

function controlsFor(fieldId: string): Array<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement> {
  return Array.from(document.querySelectorAll("input, textarea, select")).filter((element) => {
    const control = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    return control.name === fieldId || control.id === fieldId;
  }) as Array<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>;
}

function dispatchInput(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement): void {
  control.dispatchEvent(new Event("input", { bubbles: true }));
  control.dispatchEvent(new Event("change", { bubbles: true }));
}

function fillOne(payload: FillPayload): void {
  if (typeof payload.field_id !== "string" || !payload.field_id.trim()) throw new Error("field_id is required");
  if (typeof payload.action !== "string") throw new Error("action is required");
  const value = typeof payload.value === "string" ? payload.value : "";
  const controls = controlsFor(payload.field_id);
  if (!controls.length) throw new Error("field not found");

  if (payload.action === "check" || payload.action === "uncheck") {
    const target = controls.find((control) => control instanceof HTMLInputElement && (!value || control.value === value));
    if (!(target instanceof HTMLInputElement) || !["checkbox", "radio"].includes(target.type)) {
      throw new Error("field is not a checkable control");
    }
    target.checked = payload.action === "check";
    dispatchInput(target);
    return;
  }

  const control = controls[0];
  if (payload.action === "select") {
    if (!(control instanceof HTMLSelectElement)) throw new Error("field is not a native select");
    if (!Array.from(control.options).some((option) => option.value === value || option.textContent?.trim() === value)) {
      throw new Error("select option not found");
    }
    const option = Array.from(control.options).find((item) => item.value === value || item.textContent?.trim() === value);
    control.value = option?.value ?? value;
    dispatchInput(control);
    return;
  }

  if (payload.action === "set") {
    if (control instanceof HTMLSelectElement) throw new Error("select requires action=select");
    control.value = value;
    dispatchInput(control);
    return;
  }
  throw new Error("unsupported fill action");
}

// Content script is read-only in this phase. It exposes inspection only through
// message responses and never mutates the document.
chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object" || !("type" in message)) return false;
  const type = (message as { type?: unknown }).type;
  if (type === "INSPECT_FORM" || type === "GET_PAGE" || type === "READ_FORM") {
    sendResponse({ ok: true, result: inspectGreenhouse() });
    return true;
  }
  if (type === "FILL_FORM") {
    try {
      const payload = (message as { payload?: unknown }).payload;
      if (!payload || typeof payload !== "object") throw new Error("payload must be an object");
      fillOne(payload as FillPayload);
      sendResponse({ ok: true, result: inspectGreenhouse() });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
    return true;
  }
  if (type === "GET_CHALLENGE_STATE") {
    sendResponse({ ok: true, result: { state: "UNKNOWN" } });
    return true;
  }
  sendResponse({ ok: false, error: "command is not enabled in read-only phase" });
  return true;
});
