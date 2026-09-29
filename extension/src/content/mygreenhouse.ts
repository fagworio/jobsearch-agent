export type DiscoveryJobSnapshot = {
  provider: "greenhouse";
  job_id: string;
  title: string;
  company: string;
  href: string;
  remote: boolean;
  work_type: string;
  location: string;
  salary: string | null;
  posted: string;
  status: string;
  applied: boolean;
  viewed: boolean;
  description: string;
};

export type MyGreenhouseResultsSnapshot = {
  provider: "greenhouse";
  page_type: "search" | "unknown";
  surface: "document";
  url: string;
  title: string;
  ready: boolean;
  query: string;
  work_type: string[];
  jobs: DiscoveryJobSnapshot[];
};

export type DiscoveryFilterOption = {
  label: string;
  value: string;
  selected: boolean;
};

export type DiscoveryFilterSnapshot = {
  key: "location" | "date_posted" | "salary" | "work_type" | "employment_type";
  label: string;
  control: "combobox" | "radio" | "checkbox";
  parameter: string;
  selected: string[];
  options: DiscoveryFilterOption[];
};

export type MyGreenhouseFiltersSnapshot = {
  provider: "greenhouse";
  page_type: "search" | "unknown";
  surface: "document";
  url: string;
  title: string;
  ready: boolean;
  query: string;
  parameters: Record<string, string[]>;
  filters: DiscoveryFilterSnapshot[];
};

export type MyGreenhouseJobDetailsSnapshot = {
  provider: "greenhouse";
  page_type: "job" | "unknown";
  surface: "document" | "easy_apply_dialog";
  url: string;
  title: string;
  ready: boolean;
  description: string;
};

function text(element: Element | null | undefined): string {
  return element?.textContent?.replace(/\s+/g, " ").trim() ?? "";
}

function visible(element: Element): boolean {
  const style = window.getComputedStyle(element);
  const rect = element.getBoundingClientRect();
  return style.display !== "none"
    && style.visibility !== "hidden"
    && style.opacity !== "0"
    && rect.width > 0
    && rect.height > 0;
}

export function inspectMyGreenhouseJobDetails(documentRef: Document = document): MyGreenhouseJobDetailsSnapshot {
  const url = new URL(documentRef.location.href);
  const dialog = Array.from(documentRef.querySelectorAll('[role="dialog"]')).find(visible);
  const root = dialog ?? documentRef.querySelector("main") ?? documentRef.body;
  const description = (root?.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 100_000);
  const isJob = url.hostname === "my.greenhouse.io" && /^\/jobs\//.test(url.pathname);
  return {
    provider: "greenhouse",
    page_type: isJob && description ? "job" : "unknown",
    surface: dialog ? "easy_apply_dialog" : "document",
    url: url.href,
    title: documentRef.title,
    ready: isJob && Boolean(description),
    description,
  };
}

function jobId(href: string): string {
  const match = new URL(href).pathname.match(/^\/jobs\/([^/]+)\/([^/]+)/i);
  return match ? `${match[1]}:${match[2]}` : href;
}

function isSalary(value: string): boolean {
  return /[$€£]|\b(?:USD|EUR|GBP)\b|\d[\d,.]*\s*[-–]\s*\d/i.test(value);
}

function cardDescription(card: HTMLAnchorElement, title: string, company: string, tags: string[], status: string): string {
  // Cards also contain a short role description. Remove known metadata so
  // matching does not mistake a location/status label for requirements.
  let value = text(card);
  for (const token of [title, company, ...tags, status]) {
    if (token) value = value.replace(token, " ");
  }
  return value.replace(/\s+/g, " ").trim();
}

function parseJob(card: HTMLAnchorElement): DiscoveryJobSnapshot | null {
  const titleElement = card.querySelector("h4[title], h4");
  const href = card.href;
  const title = titleElement?.getAttribute("title")?.trim() || text(titleElement);
  if (!title || !href) return null;

  const heading = titleElement?.parentElement;
  const company = text(heading?.querySelector("p"));
  const tags = Array.from(card.querySelectorAll(".tag-text"))
    .map((element) => text(element))
    .filter(Boolean);
  const remote = tags.some((tag) => tag.toLowerCase() === "remote");
  const salary = tags.find(isSalary) ?? null;
  const location = tags.filter((tag) => tag.toLowerCase() !== "remote" && tag !== salary).join(", ");
  const status = text(card.querySelector("p.body__secondary"));
  const workType = tags.find((tag) => tag.toLowerCase() === "remote") ?? "";

  return {
    provider: "greenhouse",
    job_id: jobId(href),
    title,
    company,
    href,
    remote,
    work_type: workType,
    location,
    salary,
    posted: status,
    status,
    applied: /\bApplied\b/i.test(status),
    viewed: /\bViewed\b/i.test(status),
    description: cardDescription(card, title, company, tags, status),
  };
}

export function inspectMyGreenhouseResults(documentRef: Document = document): MyGreenhouseResultsSnapshot {
  const url = new URL(documentRef.location.href);
  const isSearch = url.hostname === "my.greenhouse.io" && url.pathname === "/jobs/search";
  const jobs = isSearch
    ? Array.from(documentRef.querySelectorAll('a[href*="/jobs/"]'))
      .filter((element): element is HTMLAnchorElement => element instanceof HTMLAnchorElement && Boolean(element.querySelector("h4")))
      .map(parseJob)
      .filter((job): job is DiscoveryJobSnapshot => job !== null)
    : [];

  return {
    provider: "greenhouse",
    page_type: isSearch ? "search" : "unknown",
    surface: "document",
    url: url.href,
    title: documentRef.title,
    ready: isSearch,
    query: url.searchParams.get("query") ?? "",
    work_type: url.searchParams.getAll("work_type[]"),
    jobs,
  };
}

function parametersFrom(url: URL): Record<string, string[]> {
  const parameters: Record<string, string[]> = {};
  url.searchParams.forEach((value, key) => {
    (parameters[key] ??= []).push(value);
  });
  return parameters;
}

function selectedValues(url: URL, parameter: string): string[] {
  return url.searchParams.getAll(parameter);
}

// Values captured from the live MyGreenhouse controls and then verified by
// observing the URL after applying each option. This is intentionally kept as
// a closed contract: discovery does not guess parameter names or execute an
// arbitrary filter script in the page.
const FILTER_CONTRACT: Array<Pick<DiscoveryFilterSnapshot, "key" | "label" | "control" | "parameter"> & { options: Array<Pick<DiscoveryFilterOption, "label" | "value">> }> = [
  {
    key: "date_posted",
    label: "Date posted",
    control: "radio",
    parameter: "date_posted",
    options: [
      { label: "Within 1 day", value: "past_day" },
      { label: "Within 5 days", value: "past_five_days" },
      { label: "Within 10 days", value: "past_ten_days" },
      { label: "Within 30 days", value: "past_thirty_days" },
    ],
  },
  {
    key: "salary",
    label: "Salary",
    control: "radio",
    parameter: "salary",
    options: [
      { label: "< $40,000", value: "less_than_40k" },
      { label: "$40,000+", value: "more_than_40k" },
      { label: "$60,000+", value: "more_than_60k" },
      { label: "$80,000+", value: "more_than_80k" },
      { label: "$100,000+", value: "more_than_100k" },
      { label: "$120,000+", value: "more_than_120k" },
      { label: "$140,000+", value: "more_than_140k" },
      { label: "$160,000+", value: "more_than_160k" },
      { label: "$180,000+", value: "more_than_180k" },
      { label: "$200,000+", value: "more_than_200k" },
    ],
  },
  {
    key: "work_type",
    label: "Work type",
    control: "checkbox",
    parameter: "work_type[]",
    options: [
      { label: "Remote", value: "remote" },
      { label: "Hybrid", value: "hybrid" },
      { label: "In person", value: "in_person" },
    ],
  },
  {
    key: "employment_type",
    label: "Employment type",
    control: "checkbox",
    parameter: "employment_type[]",
    options: [
      { label: "Full time", value: "full_time" },
      { label: "Part time", value: "part_time" },
      { label: "Contract", value: "contract" },
      { label: "Temporary", value: "temporary" },
      { label: "Internship", value: "internship" },
    ],
  },
];

export async function inspectMyGreenhouseFilters(documentRef: Document = document): Promise<MyGreenhouseFiltersSnapshot> {
  const url = new URL(documentRef.location.href);
  const isSearch = url.hostname === "my.greenhouse.io" && url.pathname === "/jobs/search";
  if (!isSearch) {
    return {
      provider: "greenhouse",
      page_type: "unknown",
      surface: "document",
      url: url.href,
      title: documentRef.title,
      ready: false,
      query: "",
      parameters: parametersFrom(url),
      filters: [],
    };
  }

  const filters: DiscoveryFilterSnapshot[] = [{
    key: "location",
    label: "Location",
    control: "combobox",
    parameter: "location",
    selected: selectedValues(url, "location"),
    options: Array.from(documentRef.querySelectorAll('[role="option"]'))
      .filter(visible)
      .map((option) => ({
        label: text(option),
        value: text(option),
        selected: option.getAttribute("aria-selected") === "true",
      }))
      .filter((option) => option.label),
  }];
  for (const filter of FILTER_CONTRACT) {
    filters.push({
      ...filter,
      selected: selectedValues(url, filter.parameter),
      options: filter.options.map((option) => ({
        ...option,
        selected: selectedValues(url, filter.parameter).includes(option.value),
      })),
    });
  }

  return {
    provider: "greenhouse",
    page_type: "search",
    surface: "document",
    url: url.href,
    title: documentRef.title,
    ready: true,
    query: url.searchParams.get("query") ?? "",
    parameters: parametersFrom(url),
    filters,
  };
}
