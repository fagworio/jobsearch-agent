import { activeFormRoot, fieldIdFor, formFingerprintInput, inspectAuth, inspectChallenge, inspectGreenhouse, inspectSubmitResult, isVisible, labelFor } from "./greenhouse";
import { inspectMyGreenhouseFilters, inspectMyGreenhouseJobDetails, inspectMyGreenhouseResults } from "./mygreenhouse";

type FillPayload = {
  field_id?: unknown;
  action?: unknown;
  value?: unknown;
};

type UploadPayload = {
  field_id?: unknown;
  filename?: unknown;
  mime_type?: unknown;
  bytes_base64?: unknown;
};

type SubmitAuthorization = {
  application_id?: unknown;
  form_fingerprint?: unknown;
  answers_fingerprint?: unknown;
  resume_sha256?: unknown;
  expires_at?: unknown;
  token?: unknown;
};

const consumedSubmitTokens = new Set<string>();

type FormControl = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | HTMLButtonElement;

function controlsFor(fieldId: string): FormControl[] {
  const root = activeFormRoot();
  const controls = Array.from(root.querySelectorAll("input, textarea, select, [role=combobox]")).filter((element) => {
    const control = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    const style = window.getComputedStyle(element);
    // A form control can be rendered inside a scrollable Easy Apply dialog
    // while being outside the current viewport.  `getClientRects()` (used by
    // isVisible) is too strict for that case and made a prior option-hydration
    // scroll cause later fields to report "field not found".  Keep the active
    // dialog as the scope, but accept rendered offscreen controls and still
    // reject CSS/ARIA-hidden elements.
    const rendered = style.display !== "none" && style.visibility !== "hidden";
    return (rendered || element instanceof HTMLInputElement && element.type === "file")
      && !(element instanceof HTMLButtonElement && element.getAttribute("aria-label") === "Selected country")
      && (control.getAttribute("aria-hidden") !== "true")
      && !["hidden", "submit", "button"].includes(element.getAttribute("type") ?? "");
  }) as FormControl[];
  const occurrences = new Map<string, number>();
  return controls.filter((element, index) => {
    const control = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    const label = labelFor(element, document);
    const baseId = fieldIdFor(element, label, index);
    const occurrence = (occurrences.get(baseId) ?? 0) + 1;
    occurrences.set(baseId, occurrence);
    const stableId = occurrence === 1 ? baseId : `${baseId}--${occurrence}`;
    return control.name === fieldId
      || control.id === fieldId
      || element.getAttribute("data-field-id") === fieldId
      || stableId === fieldId;
  });
}

function dispatchInput(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement): void {
  control.dispatchEvent(new Event("input", { bubbles: true }));
  control.dispatchEvent(new Event("change", { bubbles: true }));
}

function readBackValue(control: FormControl): string | boolean {
  if (control instanceof HTMLInputElement && control.getAttribute("role") === "combobox") {
    const selected = control.closest("[class*='select__control']")?.querySelector("[class*='single-value'], [class*='placeholder']");
    return selected?.textContent?.replace(/\s+/g, " ").trim() || control.value;
  }
  if (control instanceof HTMLButtonElement && control.getAttribute("role") === "combobox") {
    return control.textContent?.replace(/\s+/g, " ").trim() ?? "";
  }
  return control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)
    ? control.checked
    : control.value;
}

function normalizeText(value: string): string {
  return value.replace(/\s+/g, " ").trim().toLowerCase();
}

function visibleChoiceOptions(root: Document | Element = document): Array<{ label: string; value: string }> {
  return Array.from(root.querySelectorAll('[role="option"]'))
    .filter((element) => isVisible(element))
    .map((element) => ({
      label: element.textContent?.replace(/\s+/g, " ").trim() ?? "",
      value: element.getAttribute("data-value") || element.getAttribute("value") || element.textContent?.replace(/\s+/g, " ").trim() || "",
    }))
    .filter((option) => option.label);
}

function choiceOptionRoot(control: Element): Document | Element {
  const listboxId = control.getAttribute("aria-controls")?.trim();
  return listboxId ? document.getElementById(listboxId) ?? document : document;
}

async function inspectFieldOptions(payload: FillPayload): Promise<Record<string, unknown>> {
  if (typeof payload.field_id !== "string" || !payload.field_id.trim()) throw new Error("field_id is required");
  const controls = controlsFor(payload.field_id);
  if (!controls.length) throw new Error("field not found");
  const first = controls[0];
  if (first instanceof HTMLSelectElement) {
    const options = Array.from(first.options)
      .filter((option) => option.textContent?.trim())
      .map((option) => ({ label: option.textContent?.replace(/\s+/g, " ").trim() ?? "", value: option.value }));
    return { field_id: payload.field_id, options: options.map((option) => option.label), option_values: options };
  }
  if (first instanceof HTMLInputElement && ["checkbox", "radio"].includes(first.type)) {
    const options = controls
      .filter((control): control is HTMLInputElement => control instanceof HTMLInputElement)
      .map((control) => ({ label: labelFor(control, document) || control.value, value: control.value }))
      .filter((option) => option.label && option.value);
    return { field_id: payload.field_id, options: options.map((option) => option.label), option_values: options };
  }
  if (first.getAttribute("role") !== "combobox") throw new Error("field does not expose choices");
  const trigger = first instanceof HTMLInputElement
    ? first.closest('[class*="select__control"]') ?? first
    : first;
  trigger.scrollIntoView({ block: "center", inline: "nearest" });
  for (const type of ["mousedown", "mouseup", "click"] as const) {
    trigger.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: 1, detail: 1, view: window }));
  }
  await new Promise((resolve) => window.setTimeout(resolve, 100));
  const options = visibleChoiceOptions(choiceOptionRoot(first));
  trigger.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  trigger.dispatchEvent(new Event("blur", { bubbles: true }));
  document.body.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
  await new Promise((resolve) => window.setTimeout(resolve, 50));
  if (!options.length) throw new Error("choice options are not visible");
  return { field_id: payload.field_id, options: options.map((option) => option.label), option_values: options };
}

function ensureReadBack(control: FormControl, expected: string, checked?: boolean): void {
  const actual = readBackValue(control);
  const matches = typeof checked === "boolean"
    ? actual === checked
    : normalizeText(String(actual)) === normalizeText(expected);
  if (!matches) {
    throw new Error(`FIELD_MISMATCH: expected ${expected || String(checked)} but read ${String(actual)}`);
  }
}

async function selectCombobox(control: HTMLInputElement, value: string): Promise<void> {
  // React Select handles opening on its control wrapper. Calling click() on
  // the transparent input alone only focuses it and leaves the menu closed.
  // It also relies on the complete pointer sequence; a click on the internal
  // indicator button alone does not transition the controlled component.
  const normalize = (text: string): string => text.replace(/\s+/g, "").trim().toLowerCase();
  if (normalize(String(readBackValue(control))) === normalize(value) || normalize(control.value) === normalize(value)) return;
  const selectControl = control.closest('[class*="select__control"]');
  if (selectControl instanceof HTMLElement) {
    selectControl.scrollIntoView({ block: "center", inline: "nearest" });
    control.focus();
    for (const type of ["mousedown", "mouseup", "click"] as const) {
      selectControl.dispatchEvent(new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        button: 0,
        buttons: 1,
        detail: 1,
        view: window,
      }));
    }
  } else control.click();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(control, value);
  control.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
  await new Promise((resolve) => window.setTimeout(resolve, 100));
  // React Select renders its menu in a document-level portal by default.
  // The control remains inside the Easy Apply dialog, but the options do not.
  // Search the document for the active visible option while keeping the
  // control lookup scoped to the active form root.
  const normalizeOption = (text: string): string => text.replace(/\s+/g, " ").trim().toLowerCase();
  let option: HTMLElement | undefined;
  for (let attempt = 0; attempt < 20 && !option; attempt += 1) {
    option = Array.from(choiceOptionRoot(control).querySelectorAll('[role="option"]'))
      .filter(isVisible)
      .find((element) => normalizeOption(element.textContent ?? "") === normalizeOption(value)) as HTMLElement | undefined;
    if (!option) await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
  if (!(option instanceof HTMLElement)) {
    const diagnostics = {
      field_id: control.id || control.name || "",
      input_value: control.value,
      expanded: control.getAttribute("aria-expanded") === "true",
      aria_controls: control.getAttribute("aria-controls") || "",
      visible_options: Array.from(choiceOptionRoot(control).querySelectorAll('[role="option"]'))
        .filter((element) => isVisible(element))
        .map((element) => element.textContent?.replace(/\s+/g, " ").trim() ?? "")
        .filter(Boolean),
    };
    throw new Error(`structured combobox option not found: ${JSON.stringify(diagnostics)}`);
  }
  option.click();
  // The dedicated Chrome tab may be backgrounded. A requestAnimationFrame
  // callback can then be throttled indefinitely, leaving the native request
  // without a response even though React Select already committed the choice.
  await new Promise((resolve) => window.setTimeout(resolve, 50));
  ensureReadBack(control, value);
}

async function fillOne(payload: FillPayload): Promise<void> {
  if (typeof payload.field_id !== "string" || !payload.field_id.trim()) throw new Error("field_id is required");
  if (typeof payload.action !== "string") throw new Error("action is required");
  const value = typeof payload.value === "string" ? payload.value : "";
  const controls = controlsFor(payload.field_id);
  if (!controls.length) throw new Error("field not found");

  if (payload.action === "check" || payload.action === "uncheck" || payload.action === "check_group") {
    const target = controls.find((control) => control instanceof HTMLInputElement && (!value
      || control.value === value
      || normalizeText(labelFor(control, document)) === normalizeText(value)));
    if (!(target instanceof HTMLInputElement) || !["checkbox", "radio"].includes(target.type)) {
      throw new Error("field is not a checkable control");
    }
    const desired = payload.action !== "uncheck";
    // React-controlled checkbox groups must receive a real click so their
    // internal state updates; assigning `.checked` plus synthetic input events
    // can look correct in read-back while the provider still rejects the
    // group on submit.
    if (target.checked !== desired) target.click();
    ensureReadBack(target, value, payload.action === "check" || payload.action === "check_group");
    return;
  }

  const control = controls[0];
  if (payload.action === "select" && control instanceof HTMLButtonElement && control.getAttribute("role") === "combobox") {
    control.scrollIntoView({ block: "center", inline: "nearest" });
    for (const type of ["mousedown", "mouseup", "click"] as const) {
      control.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, buttons: 1, detail: 1, view: window }));
    }
    const normalize = (text: string): string => text.replace(/\s+/g, " ").trim().toLowerCase();
    const option = Array.from(choiceOptionRoot(control).querySelectorAll('[role="option"]'))
      .filter((element) => isVisible(element))
      .find((element) => normalize(element.textContent ?? "") === normalize(value));
    if (!(option instanceof HTMLElement)) {
      throw new Error(`structured combobox option not found: ${JSON.stringify({
        field_id: control.id || "",
        expanded: control.getAttribute("aria-expanded") === "true",
        aria_controls: control.getAttribute("aria-controls") || "",
        visible_options: Array.from(choiceOptionRoot(control).querySelectorAll('[role="option"]'))
          .filter((element) => isVisible(element))
          .map((element) => element.textContent?.replace(/\s+/g, " ").trim() ?? "")
          .filter(Boolean),
      })}`);
    }
    option.click();
    ensureReadBack(control, value);
    return;
  }
  if (payload.action === "select") {
    if (control instanceof HTMLInputElement && control.getAttribute("role") === "combobox") {
      await selectCombobox(control, value);
      return;
    }
    if (!(control instanceof HTMLSelectElement)) throw new Error("field is not a native select");
    if (!Array.from(control.options).some((option) => option.value === value || option.textContent?.trim() === value)) {
      throw new Error("select option not found");
    }
    const option = Array.from(control.options).find((item) => item.value === value || item.textContent?.trim() === value);
    control.value = option?.value ?? value;
    dispatchInput(control);
    ensureReadBack(control, control.value);
    return;
  }

  if (payload.action === "set") {
    if (control instanceof HTMLSelectElement || control instanceof HTMLButtonElement) {
      throw new Error("select controls require action=select");
    }
    control.value = value;
    dispatchInput(control);
    ensureReadBack(control, value);
    return;
  }
  throw new Error("unsupported fill action");
}

function uploadOne(payload: UploadPayload): void {
  if (typeof payload.field_id !== "string" || !payload.field_id.trim()) throw new Error("field_id is required");
  if (typeof payload.filename !== "string" || !payload.filename.trim()) throw new Error("filename is required");
  if (payload.mime_type !== "application/pdf") throw new Error("only PDF uploads are enabled");
  if (typeof payload.bytes_base64 !== "string" || payload.bytes_base64.length > 3_500_000) throw new Error("invalid artifact bytes");
  const target = controlsFor(payload.field_id).find((control) => control instanceof HTMLInputElement && control.type === "file");
  if (!(target instanceof HTMLInputElement)) throw new Error("file field not found");
  const raw = atob(payload.bytes_base64);
  const bytes = Uint8Array.from(raw, (character) => character.charCodeAt(0));
  if (bytes.length < 5 || new TextDecoder().decode(bytes.slice(0, 5)) !== "%PDF-") throw new Error("artifact is not a PDF");
  const file = new File([bytes], payload.filename, { type: "application/pdf" });
  const transfer = new DataTransfer();
  transfer.items.add(file);
  target.files = transfer.files;
  dispatchInput(target);
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(",")}}`;
}

async function sha256(value: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(canonical(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function requiredFieldsMissing(snapshot: ReturnType<typeof inspectGreenhouse>): string[] {
  return snapshot.fields
    .filter((field) => field.required && (!field.value.trim() || /^select(?:\.\.\.)?$/i.test(field.value.trim())))
    .map((field) => field.id);
}

async function waitForApplication(): Promise<Record<string, unknown>> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const snapshot = inspectGreenhouse();
    if (snapshot.page_type === "application" && snapshot.ready) return snapshot;
    if (snapshot.page_type !== "application") {
      const apply = Array.from(document.querySelectorAll("button, a, [role=button]"))
        .filter(isVisible)
        .find((element) => {
          const text = element.textContent?.replace(/\s+/g, " ").trim() ?? "";
          if (/\b(?:already\s+)?applied\b|view application/i.test(text)) return false;
          return /easy apply|apply now|apply for this job|apply/i.test(text)
            && !/submit application/i.test(text);
        });
      if (apply instanceof HTMLElement) apply.click();
    }
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  throw new Error("FORM_NOT_FOUND");
}

function removeRepeatableEntry(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== "object") throw new Error("payload must be an object");
  const input = payload as { entry_type?: unknown; index?: unknown };
  if (input.entry_type !== "education" || !Number.isInteger(input.index) || (input.index as number) < 0) {
    throw new Error("only an indexed education entry can be removed");
  }
  const entries = Array.from(document.querySelectorAll('[role="dialog"] button[aria-label="Remove"]'))
    .filter(isVisible);
  const target = entries[input.index as number];
  if (!(target instanceof HTMLButtonElement)) throw new Error("repeatable education entry not found");
  target.click();
  return inspectGreenhouse();
}

async function requestSubmit(payload: { authorization?: unknown }): Promise<Record<string, unknown>> {
  if (!payload.authorization || typeof payload.authorization !== "object") throw new Error("authorization is required");
  const authorization = payload.authorization as SubmitAuthorization;
  const strings = ["application_id", "form_fingerprint", "answers_fingerprint", "resume_sha256", "expires_at", "token"] as const;
  for (const key of strings) {
    if (typeof authorization[key] !== "string" || !authorization[key]?.trim()) throw new Error(`${key} is required`);
  }
  const token = authorization.token as string;
  if (consumedSubmitTokens.has(token)) throw new Error("submit authorization was already used");
  const expiry = Date.parse(authorization.expires_at as string);
  if (!Number.isFinite(expiry) || Date.now() >= expiry) throw new Error("submit authorization expired");

  const challenge = inspectChallenge();
  if (challenge.state !== "CLEAR") throw new Error(`human challenge state is ${challenge.state}`);
  const snapshot = inspectGreenhouse();
  if (snapshot.page_type !== "application" || !snapshot.ready) throw new Error("application form is not ready");
  const missing = requiredFieldsMissing(snapshot);
  if (missing.length) throw new Error(`required fields are missing: ${missing.join(", ")}`);
  const actualFormFingerprint = await sha256(formFingerprintInput(snapshot));
  if (actualFormFingerprint !== authorization.form_fingerprint) throw new Error("form fingerprint does not match");
  const buttons = Array.from(document.querySelectorAll("#btn-submit, button[type=submit], input[type=submit]"))
    .filter((element) => isVisible(element));
  if (buttons.length !== 1) throw new Error("submit control is not uniquely identified");

  consumedSubmitTokens.add(token);
  (buttons[0] as HTMLElement).click();
  return { submitted: true, token, application_id: authorization.application_id };
}

// Mutating commands remain typed and narrowly scoped; there is no arbitrary
// script execution path. Submission is separately guarded by authorization.
chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
  if (!message || typeof message !== "object" || !("type" in message)) return false;
  const type = (message as { type?: unknown }).type;
  if (type === "GET_AUTH_STATE") {
    sendResponse({ ok: true, result: inspectAuth() });
    return true;
  }
  if (type === "INSPECT_FORM" || type === "GET_PAGE" || type === "READ_FORM") {
    sendResponse({ ok: true, result: inspectGreenhouse() });
    return true;
  }
  if (type === "GET_FIELD_OPTIONS") {
    void inspectFieldOptions(((message as { payload?: unknown }).payload ?? {}) as FillPayload)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (type === "WAIT_FOR_APPLICATION") {
    void waitForApplication()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (type === "REMOVE_REPEATABLE_ENTRY") {
    try {
      const payload = (message as { payload?: unknown }).payload;
      sendResponse({ ok: true, result: removeRepeatableEntry(payload) });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
    return true;
  }
  if (type === "GET_DISCOVERY_RESULTS") {
    sendResponse({ ok: true, result: inspectMyGreenhouseResults() });
    return true;
  }
  if (type === "GET_JOB_DETAILS") {
    sendResponse({ ok: true, result: inspectMyGreenhouseJobDetails() });
    return true;
  }
  if (type === "GET_DISCOVERY_FILTERS") {
    void inspectMyGreenhouseFilters()
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (type === "FILL_FORM") {
    try {
      const payload = (message as { payload?: unknown }).payload;
      if (!payload || typeof payload !== "object") throw new Error("payload must be an object");
      void fillOne(payload as FillPayload)
        .then(() => sendResponse({ ok: true, result: inspectGreenhouse() }))
        .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
    } catch (error) { sendResponse({ ok: false, error: String(error) }); }
    return true;
  }
  if (type === "UPLOAD_ARTIFACT") {
    try {
      const payload = (message as { payload?: unknown }).payload;
      if (!payload || typeof payload !== "object") throw new Error("payload must be an object");
      uploadOne(payload as UploadPayload);
      sendResponse({ ok: true, result: inspectGreenhouse() });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
    return true;
  }
  if (type === "GET_CHALLENGE_STATE") {
    sendResponse({ ok: true, result: inspectChallenge() });
    return true;
  }
  if (type === "REQUEST_SUBMIT") {
    void requestSubmit(((message as { payload?: unknown }).payload ?? {}) as { authorization?: unknown })
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error: unknown) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (type === "GET_SUBMIT_RESULT") {
    sendResponse({ ok: true, result: inspectSubmitResult() });
    return true;
  }
  sendResponse({ ok: false, error: "command is not enabled in read-only phase" });
  return true;
});

// Wakes the service worker on a real Greenhouse tab so it can open the
// Native Messaging host before the Python backend connects.
void chrome.runtime.sendMessage({ type: "EXTENSION_READY" }).catch(() => undefined);
