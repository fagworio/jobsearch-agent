export type FieldSnapshot = {
  id: string;
  type: string;
  label: string;
  required: boolean;
  options: string[];
  option_values?: Array<{ label: string; value: string }>;
  value: string;
  checked: boolean;
  structured?: StructuredControlSnapshot;
};

export type StructuredControlSnapshot = {
  kind: "combobox";
  input_id: string;
  control_id: string;
  listbox_id: string;
  expanded: boolean;
  visible_options: string[];
  related_hidden_fields: string[];
};

export type PageSnapshot = {
  provider: "greenhouse";
  page_type: "application" | "job" | "unknown";
  surface: "document" | "easy_apply_dialog";
  url: string;
  title: string;
  ready: boolean;
  fields: FieldSnapshot[];
};

export type ChallengeSnapshot = {
  state: "CLEAR" | "BLOCKING" | "UNKNOWN";
  signals: string[];
};

export type AuthSnapshot = {
  provider: "greenhouse";
  state: "LOGIN_REQUIRED" | "LOGIN_PENDING" | "AUTHENTICATED_MANUAL" | "UNKNOWN" | "NOT_MY_GREENHOUSE";
  url: string;
  title: string;
  detail: string;
};

export type SubmitResultSnapshot = {
  state: "SUBMITTED" | "SUBMIT_FAILED" | "SUBMIT_UNKNOWN";
  primary: {
    url: string;
    confirmation_component: boolean;
    error_component: boolean;
    detail: string;
  };
  secondary: Array<{
    source: "confirmation_email" | "mygreenhouse_status";
    reference: string;
    observed_at: string;
    detail: string;
  }>;
};

export type FormFingerprintInput = {
  provider: "greenhouse";
  page_type: "application";
  fields: Array<Pick<FieldSnapshot, "id" | "type" | "label" | "required" | "options">>;
};

export type FormRoot = Document | HTMLElement;

function rootDocument(root: FormRoot): Document {
  return root instanceof Document ? root : root.ownerDocument;
}

export function activeFormRoot(documentRef: Document = document): FormRoot {
  const dialog = Array.from(documentRef.querySelectorAll('[role="dialog"]'))
    .filter(isVisible)
    .find((candidate) => candidate.querySelector("form, button[type=submit], input[id=first_name], input[id=last_name]"));
  return dialog instanceof HTMLElement ? dialog : documentRef;
}

export function labelFor(element: Element, documentRef: Document): string {
  const labelledBy = element.getAttribute("aria-labelledby");
  if (labelledBy) {
    const text = labelledBy.split(/\s+/)
      .map((id) => documentRef.getElementById(id)?.textContent ?? "")
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    if (text) return text;
  }
  const ariaLabel = element.getAttribute("aria-label")?.trim();
  if (ariaLabel) return ariaLabel;
  const id = element.getAttribute("id");
  if (id) {
    const label = documentRef.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (label?.textContent) return label.textContent.replace(/\s+/g, " ").trim();
  }
  const parent = element.closest("label");
  if (parent?.textContent) return parent.textContent.replace(/\s+/g, " ").trim();

  const wrapper = element.closest(".field-wrapper, .input-wrapper--with-label, fieldset");
  const wrapperLabel = wrapper?.querySelector("label, legend");
  return wrapperLabel?.textContent?.replace(/\s+/g, " ").trim() ?? "";
}

function choiceLabelFor(element: HTMLInputElement, documentRef: Document): string {
  const label = labelFor(element, documentRef);
  if (label && label !== element.name && label !== element.id) return label;
  return element.value;
}

function choiceOptions(elements: HTMLInputElement[], documentRef: Document): Array<{ label: string; value: string }> {
  return elements
    .map((input) => ({ label: choiceLabelFor(input, documentRef), value: input.value }))
    .filter((option) => option.label && option.value)
    .filter((option, index, all) => all.findIndex((candidate) => candidate.value === option.value) === index);
}

function optionsFor(element: Element, documentRef: Document): string[] {
  if (element instanceof HTMLSelectElement) {
    return Array.from(element.options).map((option) => option.textContent?.trim() ?? "").filter(Boolean);
  }
  return Array.from(element.parentElement?.querySelectorAll("input[type=radio], input[type=checkbox]") ?? [])
    .map((input) => choiceLabelFor(input as HTMLInputElement, documentRef))
    .filter(Boolean);
}

function structuredFor(element: Element, documentRef: Document): StructuredControlSnapshot | undefined {
  if (element.getAttribute("role") !== "combobox") return undefined;
  const inputId = element.getAttribute("id") ?? "";
  const control = element.closest('[class*="select__control"]');
  const controlId = control?.getAttribute("id") ?? "";
  const listboxId = element.getAttribute("aria-controls") ?? "";
  const scope = element.closest(".field-wrapper, .input-wrapper, [data-field-id], [role=group]") ?? element.parentElement;
  const relatedHiddenFields = Array.from(scope?.querySelectorAll('input[type="hidden"]') ?? [])
    .map((hidden) => hidden.getAttribute("name") || hidden.getAttribute("id") || "")
    .filter(Boolean);
  const visibleOptions = Array.from(documentRef.querySelectorAll('[role="option"]'))
    .filter(isVisible)
    .map((option) => option.textContent?.replace(/\s+/g, " ").trim() ?? "")
    .filter(Boolean);
  return {
    kind: "combobox",
    input_id: inputId,
    control_id: controlId,
    listbox_id: listboxId,
    expanded: element.getAttribute("aria-expanded") === "true",
    visible_options: Array.from(new Set(visibleOptions)),
    related_hidden_fields: Array.from(new Set(relatedHiddenFields)),
  };
}

function valueFor(element: Element): string {
  if (element instanceof HTMLInputElement && element.getAttribute("role") === "combobox") {
    const control = element.closest('[class*="select__control"]');
    const selected = control?.querySelector('[class*="single-value"], [class*="placeholder"]');
    return selected?.textContent?.replace(/\s+/g, " ").trim() || element.value;
  }
  if (element instanceof HTMLButtonElement && element.getAttribute("role") === "combobox") {
    return element.textContent?.replace(/\s+/g, " ").trim() ?? "";
  }
  if (element instanceof HTMLInputElement && ["checkbox", "radio"].includes(element.type)) {
    return element.checked ? element.value : "";
  }
  return (element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement).value;
}

export function fieldIdFor(element: Element, label: string, index: number): string {
  const stableAttribute = ["name", "id", "data-field-id", "data-testid"]
    .map((attribute) => element.getAttribute(attribute)?.trim() ?? "")
    .find((value) => value && !/^react-select-\d+-input$/.test(value));
  if (stableAttribute) return stableAttribute;
  const slug = label
    .replace(/\*/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-|-$/g, "");
  return slug ? `label:${slug}` : `field-${index + 1}`;
}

function requiredFor(element: Element, label: string): boolean {
  return element.hasAttribute("required")
    || element.getAttribute("aria-required") === "true"
    || /\*/.test(label);
}

function radioGroupLabel(radios: HTMLInputElement[], documentRef: Document): string {
  const first = radios[0];
  const fieldset = first.closest("fieldset");
  const legend = fieldset?.querySelector("legend");
  if (legend?.textContent?.trim()) return legend.textContent.replace(/\s+/g, " ").trim();

  const labelledBy = first.getAttribute("aria-labelledby");
  if (labelledBy) {
    const text = labelledBy.split(/\s+/)
      .map((id) => documentRef.getElementById(id)?.textContent ?? "")
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
    if (text) return text;
  }

  const group = first.closest('[role="radiogroup"]');
  const groupLabel = group?.getAttribute("aria-label")?.trim();
  if (groupLabel) return groupLabel;
  return labelFor(first, documentRef);
}

function radioGroupSnapshot(radios: HTMLInputElement[], index: number, documentRef: Document): FieldSnapshot {
  const first = radios[0];
  const selected = radios.find((radio) => radio.checked);
  const optionValues = choiceOptions(radios, documentRef);
  return {
    id: first.name || first.id || `field-${index + 1}`,
    type: "radio",
    label: radioGroupLabel(radios, documentRef),
    required: radios.some((radio) => radio.required || radio.getAttribute("aria-required") === "true"),
    options: optionValues.map((option) => option.label),
    option_values: optionValues,
    value: selected?.value ?? "",
    checked: Boolean(selected),
  };
}

function checkboxGroupLabel(checkboxes: HTMLInputElement[], documentRef: Document): string {
  return radioGroupLabel(checkboxes, documentRef);
}

function checkboxGroupSnapshot(checkboxes: HTMLInputElement[], index: number, documentRef: Document): FieldSnapshot {
  const first = checkboxes[0];
  const optionValues = choiceOptions(checkboxes, documentRef);
  const selected = new Set(checkboxes.filter((checkbox) => checkbox.checked).map((checkbox) => checkbox.value));
  return {
    id: first.name || first.id || `field-${index + 1}`,
    type: "checkbox_group",
    label: checkboxGroupLabel(checkboxes, documentRef),
    required: checkboxes.some((checkbox) => checkbox.required || checkbox.getAttribute("aria-required") === "true"),
    options: optionValues.map((option) => option.label),
    option_values: optionValues,
    value: optionValues.filter((option) => selected.has(option.value)).map((option) => option.label).join(", "),
    checked: selected.size > 0,
  };
}

export function inspectGreenhouse(documentRef: Document = document): PageSnapshot {
  const root = activeFormRoot(documentRef);
  const controls = Array.from(root.querySelectorAll("input, textarea, select, [role=combobox]"))
    .filter((element) => !["hidden", "submit", "button"].includes(element.getAttribute("type") ?? ""))
    .filter((element) => element.getAttribute("aria-hidden") !== "true")
    .filter((element) => isVisible(element) || element instanceof HTMLInputElement && element.type === "file")
    .filter((element) => !(element instanceof HTMLButtonElement && element.getAttribute("aria-label") === "Selected country"));
  const fields: FieldSnapshot[] = [];
  const seenRadioGroups = new Set<string>();
  const seenCheckboxGroups = new Set<string>();
  const fieldOccurrences = new Map<string, number>();
  controls.forEach((element, index) => {
    const input = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    if (input instanceof HTMLInputElement && ["radio", "checkbox"].includes(input.type) && input.name) {
      const groupKey = input.name || input.id || `field-${index + 1}`;
      const group = controls.filter((candidate): candidate is HTMLInputElement => {
        return candidate instanceof HTMLInputElement
          && candidate.type === input.type
          && (candidate.name || candidate.id || `field-${index + 1}`) === groupKey;
      });
      if (input.type === "radio" && group.length > 1) {
        if (seenRadioGroups.has(groupKey)) return;
        seenRadioGroups.add(groupKey);
        fields.push(radioGroupSnapshot(group, index, documentRef));
        return;
      }
      if (input.type === "checkbox" && group.length > 1) {
        if (seenCheckboxGroups.has(groupKey)) return;
        seenCheckboxGroups.add(groupKey);
        fields.push(checkboxGroupSnapshot(group, index, documentRef));
        return;
      }
    }
    const role = element.getAttribute("role");
    const label = labelFor(element, rootDocument(root));
    const baseId = fieldIdFor(element, label, index);
    const occurrence = (fieldOccurrences.get(baseId) ?? 0) + 1;
    fieldOccurrences.set(baseId, occurrence);
    fields.push({
      id: occurrence === 1 ? baseId : `${baseId}--${occurrence}`,
      type: role === "combobox" ? "combobox" : input.getAttribute("type") || element.tagName.toLowerCase(),
      label,
      required: requiredFor(element, label),
      options: optionsFor(element, documentRef),
      value: valueFor(element),
      checked: input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type)
        ? input.checked
        : false,
      structured: structuredFor(element, rootDocument(root)),
    });
  });
  // Easy Apply removes the file input after a resume is attached and leaves
  // only the filename plus a visible "Remove file" control. Preserve that
  // state in the neutral snapshot so the backend can verify an existing
  // attachment instead of trying to upload a second file into a missing
  // input.
  if (!fields.some((field) => field.id === "resume" || /resume|cv/i.test(field.label))) {
    const removeFile = Array.from(root.querySelectorAll("button"))
      .find((element) => isVisible(element) && /remove file|remover arquivo/i.test(element.getAttribute("aria-label") ?? element.textContent ?? ""));
    if (removeFile) {
      const wrapperText = removeFile.parentElement?.textContent?.replace(/\s+/g, " ").trim() ?? "";
      const filename = wrapperText.replace(/remove file|remover arquivo/ig, "").trim() || "attached";
      fields.push({
        id: "resume",
        type: "file",
        label: "Resume/CV*",
        required: true,
        options: [],
        value: filename,
        checked: false,
      });
    }
  }
  const application = root.querySelector("#application-form, #application_form, form.application--form") !== null
    || fields.some((field) => field.id.startsWith("job_application["));
  return {
    provider: "greenhouse",
    page_type: application ? "application" : fields.length ? "job" : "unknown",
    surface: root === documentRef ? "document" : "easy_apply_dialog",
    url: location.href,
    title: documentRef.title,
    ready: application && fields.length > 0,
    fields,
  };
}

export function formFingerprintInput(snapshot: PageSnapshot): FormFingerprintInput {
  return {
    provider: "greenhouse",
    page_type: "application",
    fields: snapshot.fields.map(({ id, type, label, required, options }) => ({ id, type, label, required, options })),
  };
}

export function isVisible(element: Element): boolean {
  const style = window.getComputedStyle(element);
  return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
}

export function inspectChallenge(documentRef: Document = document): ChallengeSnapshot {
  const signals: string[] = [];
  const selectors: Array<[string, string]> = [
    ['iframe[src*="hcaptcha"]', "visible-hcaptcha-iframe"],
    ['iframe[src*="recaptcha"]', "visible-recaptcha-iframe"],
    [".h-captcha", "visible-hcaptcha-widget"],
    [".g-recaptcha", "visible-recaptcha-widget"],
    ["[data-sitekey]", "visible-challenge-widget"],
  ];
  for (const [selector, signal] of selectors) {
    if (Array.from(documentRef.querySelectorAll(selector)).some(isVisible)) signals.push(signal);
  }
  const application = documentRef.querySelector("#application-form, #application_form") !== null;
  const solved = Array.from(documentRef.querySelectorAll(
    'textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"], [data-captcha-state="solved"]',
  )).some((element) => {
    if (element instanceof HTMLTextAreaElement) return element.value.trim().length > 0;
    return isVisible(element);
  });
  return {
    state: solved || !signals.length && application ? "CLEAR" : signals.length ? "BLOCKING" : "UNKNOWN",
    signals: solved ? signals.filter((signal) => !signal.includes("iframe")) : signals,
  };
}

function visibleText(documentRef: Document): string {
  return (documentRef.body?.innerText ?? "").replace(/\s+/g, " ").trim();
}

function hasVisibleSignOut(documentRef: Document): boolean {
  const signOutLink = Array.from(documentRef.querySelectorAll('a[href*="sign_out"], button'))
    .filter(isVisible)
    .some((element) => /sign out|log out|sair/i.test(element.textContent ?? ""));
  return signOutLink || /\b(sign out|log out|sair)\b/i.test(visibleText(documentRef));
}

export function inspectAuth(documentRef: Document = document): AuthSnapshot {
  const url = location.href;
  const title = documentRef.title;
  if (location.hostname !== "my.greenhouse.io") {
    return { provider: "greenhouse", state: "NOT_MY_GREENHOUSE", url, title, detail: "active tab is not MyGreenhouse" };
  }

  const signInPath = /^\/users\/sign_in\/?$/.test(location.pathname);
  const emailInput = Array.from(documentRef.querySelectorAll("input"))
    .filter(isVisible)
    .some((element) => {
      const input = element as HTMLInputElement;
      return input.type === "email" || input.autocomplete === "username";
    });
  const text = visibleText(documentRef);
  if (signInPath && /security code|verification code|enter code/i.test(text) && !emailInput) {
    return { provider: "greenhouse", state: "LOGIN_PENDING", url, title, detail: "security code is required" };
  }
  if (signInPath && emailInput) {
    return { provider: "greenhouse", state: "LOGIN_REQUIRED", url, title, detail: "manual MyGreenhouse login is required" };
  }
  if (hasVisibleSignOut(documentRef)) {
    return { provider: "greenhouse", state: "AUTHENTICATED_MANUAL", url, title, detail: "authenticated browser session observed" };
  }
  if (/^\/dashboard\/?$/.test(location.pathname)) {
    return { provider: "greenhouse", state: "AUTHENTICATED_MANUAL", url, title, detail: "private MyGreenhouse dashboard observed" };
  }
  return { provider: "greenhouse", state: "UNKNOWN", url, title, detail: "MyGreenhouse page does not expose a decisive auth signal" };
}

export function inspectSubmitResult(documentRef: Document = document): SubmitResultSnapshot {
  const bodyText = documentRef.body?.innerText ?? "";
  const confirmationText = /thank you for applying|application (?:has been|was) (?:submitted|received)|we(?:'ve| have) received your application/i.test(bodyText);
  const confirmationComponent = Array.from(documentRef.querySelectorAll(
    '[data-application-status="submitted"], .application-submitted, #confirmation',
  )).some(isVisible) || confirmationText;
  const errorNodes = Array.from(documentRef.querySelectorAll(
    ".errors, .error-message, [aria-invalid=\"true\"]",
  )).filter(isVisible);
  const errorComponent = errorNodes.length > 0;
  const errorDetail = errorNodes
    .map((element) => {
      const text = element.textContent?.replace(/\s+/g, " ").trim() ?? "";
      if (text) return text;
      const control = element.matches("input, textarea, select, [role=combobox]") ? element : element.querySelector("input, textarea, select, [role=combobox]");
      if (control instanceof HTMLInputElement || control instanceof HTMLTextAreaElement || control instanceof HTMLSelectElement) {
        return `${labelFor(control, documentRef) || control.name || control.id}: invalid value`;
      }
      return element.outerHTML.replace(/\s+/g, " ").slice(0, 300);
    })
    .filter(Boolean)
    .join(" | ")
    .slice(0, 1000);
  return {
    state: confirmationComponent ? "SUBMITTED" : errorComponent ? "SUBMIT_FAILED" : "SUBMIT_UNKNOWN",
    primary: {
      url: location.href,
      confirmation_component: confirmationComponent,
      error_component: errorComponent,
      detail: confirmationComponent
        ? "visible confirmation evidence"
        : errorComponent
          ? errorDetail || "visible error evidence"
          : "no decisive page evidence",
    },
    secondary: [],
  };
}
