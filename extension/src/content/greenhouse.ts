export type FieldSnapshot = {
  id: string;
  type: string;
  label: string;
  required: boolean;
  options: string[];
  value: string;
};

export type PageSnapshot = {
  provider: "greenhouse";
  page_type: "application" | "job" | "unknown";
  url: string;
  title: string;
  ready: boolean;
  fields: FieldSnapshot[];
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
      value: input.value,
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
