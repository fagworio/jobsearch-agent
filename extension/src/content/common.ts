import { formFingerprintInput, inspectChallenge, inspectGreenhouse, inspectSubmitResult, isVisible } from "./greenhouse";

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

function readBackValue(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement): string | boolean {
  if (control instanceof HTMLInputElement && control.getAttribute("role") === "combobox") {
    const selected = control.closest("[class*='select__control']")?.querySelector("[class*='single-value']");
    return selected?.textContent?.replace(/\s+/g, " ").trim() || control.value;
  }
  return control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)
    ? control.checked
    : control.value;
}

function ensureReadBack(control: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, expected: string, checked?: boolean): void {
  const actual = readBackValue(control);
  if (typeof checked === "boolean" ? actual !== checked : actual !== expected) {
    throw new Error(`FIELD_MISMATCH: expected ${expected || String(checked)} but read ${String(actual)}`);
  }
}

async function selectCombobox(control: HTMLInputElement, value: string): Promise<void> {
  control.focus();
  control.click();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(control, value);
  control.dispatchEvent(new InputEvent("input", { bubbles: true, data: value, inputType: "insertText" }));
  await new Promise((resolve) => window.setTimeout(resolve, 100));
  const option = Array.from(document.querySelectorAll('[role="option"]'))
    .filter((element) => isVisible(element))
    .find((element) => element.textContent?.replace(/\s+/g, " ").trim() === value);
  if (!(option instanceof HTMLElement)) throw new Error("combobox option not found");
  option.click();
  await new Promise((resolve) => window.requestAnimationFrame(() => resolve(undefined)));
  ensureReadBack(control, value);
}

async function fillOne(payload: FillPayload): Promise<void> {
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
    ensureReadBack(target, value, payload.action === "check");
    return;
  }

  const control = controls[0];
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
    if (control instanceof HTMLSelectElement) throw new Error("select requires action=select");
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
    .filter((field) => field.required && !field.value.trim())
    .map((field) => field.id);
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
  if (type === "INSPECT_FORM" || type === "GET_PAGE" || type === "READ_FORM") {
    sendResponse({ ok: true, result: inspectGreenhouse() });
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
