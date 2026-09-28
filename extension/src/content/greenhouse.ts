export type FieldSnapshot = {
  id: string;
  type: string;
  label: string;
  required: boolean;
  options: string[];
  value: string;
  checked: boolean;
};

export type PageSnapshot = {
  provider: "greenhouse";
  page_type: "application" | "job" | "unknown";
  url: string;
  title: string;
  ready: boolean;
  fields: FieldSnapshot[];
};

export type ChallengeSnapshot = {
  state: "CLEAR" | "BLOCKING" | "UNKNOWN";
  signals: string[];
};

export type SubmitResultSnapshot = {
  state: "SUBMITTED" | "SUBMIT_FAILED" | "SUBMIT_UNKNOWN";
  primary: {
    url: string;
    confirmation_component: boolean;
    error_component: boolean;
    detail: string;
  };
  secondary: string[];
};

export type FormFingerprintInput = {
  provider: "greenhouse";
  page_type: "application";
  fields: Array<Pick<FieldSnapshot, "id" | "type" | "label" | "required" | "options">>;
};

function labelFor(element: Element): string {
  const id = element.getAttribute("id");
  if (id) {
    const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (label?.textContent) return label.textContent.replace(/\s+/g, " ").trim();
  }
  const parent = element.closest("label");
  return parent?.textContent?.replace(/\s+/g, " ").trim() ?? "";
}

function optionsFor(element: Element): string[] {
  if (element instanceof HTMLSelectElement) {
    return Array.from(element.options).map((option) => option.textContent?.trim() ?? "").filter(Boolean);
  }
  return Array.from(element.parentElement?.querySelectorAll("input[type=radio], input[type=checkbox]") ?? [])
    .map((input) => input.getAttribute("value") ?? "")
    .filter(Boolean);
}

export function inspectGreenhouse(documentRef: Document = document): PageSnapshot {
  const controls = Array.from(documentRef.querySelectorAll("input, textarea, select"))
    .filter((element) => !["hidden", "submit", "button"].includes(element.getAttribute("type") ?? ""));
  const fields: FieldSnapshot[] = controls.map((element, index) => {
    const input = element as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
    return {
      id: input.name || input.id || `field-${index + 1}`,
      type: input.getAttribute("type") || element.tagName.toLowerCase(),
      label: labelFor(element),
      required: input.required,
      options: optionsFor(element),
      value: input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type)
        ? (input.checked ? input.value : "")
        : input.value,
      checked: input instanceof HTMLInputElement && ["checkbox", "radio"].includes(input.type)
        ? input.checked
        : false,
    };
  });
  const application = documentRef.querySelector("#application-form, #application_form") !== null
    || fields.some((field) => field.id.startsWith("job_application["));
  return {
    provider: "greenhouse",
    page_type: application ? "application" : fields.length ? "job" : "unknown",
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
  return {
    state: signals.length ? "BLOCKING" : application ? "CLEAR" : "UNKNOWN",
    signals,
  };
}

export function inspectSubmitResult(documentRef: Document = document): SubmitResultSnapshot {
  const bodyText = documentRef.body?.innerText ?? "";
  const confirmationText = /thank you for applying|application (?:has been|was) (?:submitted|received)|we(?:'ve| have) received your application/i.test(bodyText);
  const confirmationComponent = Array.from(documentRef.querySelectorAll(
    '[data-application-status="submitted"], .application-submitted, #confirmation',
  )).some(isVisible) || confirmationText;
  const errorComponent = Array.from(documentRef.querySelectorAll(
    ".errors, .error-message, [aria-invalid=\"true\"]",
  )).some(isVisible);
  return {
    state: confirmationComponent ? "SUBMITTED" : errorComponent ? "SUBMIT_FAILED" : "SUBMIT_UNKNOWN",
    primary: {
      url: location.href,
      confirmation_component: confirmationComponent,
      error_component: errorComponent,
      detail: confirmationComponent ? "visible confirmation evidence" : errorComponent ? "visible error evidence" : "no decisive page evidence",
    },
    secondary: [],
  };
}
