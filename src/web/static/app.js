"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const hash = new URLSearchParams(location.hash.slice(1));
const initialToken = hash.get("token");
if (initialToken) {
  sessionStorage.setItem("timesheet-token", initialToken);
  history.replaceState(null, "", location.pathname);
}
const token = sessionStorage.getItem("timesheet-token");
let registry = null;
let currentDay = null;
let editable = false;
let busy = false;
let exiting = false;
const serviceFields = [
  "azure_openai_endpoint", "azure_openai_deployment_name", "azure_openai_api_version",
  "tenant_id", "client_id", "timezone",
];

function notice(message, error = false) {
  const element = $("#notice");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
}

async function api(path, options = {}) {
  if (exiting && path !== "/api/lifecycle") throw new Error("Application is exiting. Relaunch it to continue.");
  if (!token) throw new Error("Open the dashboard using the private URL printed by the server.");
  const response = await fetch(path, {
    ...options,
    headers: {"X-Timesheet-Token": token, "Content-Type": "application/json", ...options.headers},
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`Request failed (${response.status}). Check the server console.`); }
  if (!response.ok) {
    const message = typeof data.detail === "string" ? data.detail : "Invalid input. Check your fields.";
    throw new Error(message);
  }
  return data;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function switchTab(name) {
  $$(".page").forEach(page => { page.hidden = page.id !== name; });
  $$(".nav-item").forEach(button => button.classList.toggle("active", button.dataset.tab === name));
  $("#breadcrumb").textContent = {
    daily: "DAILY REVIEW", monthly: "MONTHLY EXPORT", scheduler: "SCHEDULER",
    projects: "PROJECTS", settings: "SETUP & INTEGRATIONS",
  }[name];
  if (name === "scheduler") refreshScheduler().catch(error => notice(error.message, true));
}
$$("[data-tab]").forEach(button => button.addEventListener("click", () => switchTab(button.dataset.tab)));
$(".brand").addEventListener("click", () => switchTab("daily"));

function renderMcpStatus(data) {
  const status = $("#mcp-status");
  status.replaceChildren();
  for (const [name, configured] of Object.entries(data.mcp_operations)) {
    const item = node("div", "status-item");
    item.append(node("strong", "", {emails: "Emails", events: "Calendar", chats: "Teams activity", send_review: "Review message"}[name]));
    item.append(document.createTextNode(configured ? "Configured" : "Not configured"));
    status.append(item);
  }
}

function renderProfileSetup(setup) {
  const profile = setup.profile_setup;
  const form = $("#profile-form");
  form.hidden = !profile;
  $("#template-form").hidden = !profile;
  if (!profile) return;
  for (const field of serviceFields) {
    form.elements[field].value = profile[field] ?? "";
    form.elements[field].disabled = profile.environment_overrides.includes(field);
  }
  const keyOverridden = profile.environment_overrides.includes("azure_openai_api_key");
  form.elements.azure_openai_api_key.value = "";
  form.elements.azure_openai_api_key.disabled = keyOverridden;
  form.elements.clear_api_key.checked = false;
  form.elements.clear_api_key.disabled = keyOverridden;
  $("#profile-status").textContent = profile.services_configured
    ? "Service settings are present. Connectivity has not been tested."
    : "First-run setup: enter your service settings before generating proposals.";
  $("#profile-location").textContent = `Local profile: ${profile.data_directory}`;
  $("#profile-key-status").textContent = profile.api_key_configured
    ? "An API key is configured; it is never sent back to this page."
    : "No API key is configured.";
  $("#profile-overrides").textContent = profile.environment_overrides.length
    ? `Environment-controlled fields are read-only: ${profile.environment_overrides.join(", ")}. Remove those overrides before changing them here.`
    : "Blank API key keeps the saved key. Microsoft 365 sign-in tokens are held in memory only.";
  renderTemplateStatus(profile);
}

function renderTemplateStatus(profile) {
  $("#template-file").disabled = profile.template_available;
  $("#upload-template").disabled = profile.template_available;
  $("#template-status").textContent = profile.template_available
    ? "A template for the saved layout is already available and will not be overwritten."
    : "For the saved layout, upload a template or let the first export create one. Uploading does not validate its layout.";
}

function updateLayoutFields() {
  const rows = $("#excel-form").elements.layout_mode.value === "rows";
  $("#rows-layout-fields").hidden = !rows;
  $("#columns-layout-fields").hidden = rows;
  $$("input, textarea", $("#rows-layout-fields")).forEach(field => { field.disabled = !rows; });
  $$("input, textarea", $("#columns-layout-fields")).forEach(field => { field.disabled = rows; });
}

function renderExcel(layout) {
  const form = $("#excel-form");
  for (const field of ["layout_mode", "date_format", "sheet_name", "project_column", "first_project_row", "last_project_row",
    "header_row", "month_header_cell", "month_header_format", "monthly_total_column"]) {
    form.elements[field].value = layout[field] ?? "";
  }
  form.elements.day_columns.value = layout.day_columns ? JSON.stringify(layout.day_columns, null, 2) : "";
  form.elements.write_total_formula.checked = layout.write_total_formula;
  updateLayoutFields();
}

function projectField(title, key, value, placeholder = "") {
  const label = node("label", "", title);
  const input = node("input");
  input.dataset.key = key;
  input.value = Array.isArray(value) ? value.join(", ") : (value ?? "");
  input.placeholder = placeholder;
  if (key === "code" || key === "client") input.required = true;
  label.append(input);
  return label;
}

function addProject(project = {}) {
  const card = node("div", "project-card");
  const header = node("div", "section-title");
  header.append(node("h3", "", "Project details"));
  const remove = node("button", "button quiet", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => card.remove());
  header.append(remove);
  const fields = node("div", "form-grid");
  fields.append(
    projectField("Project code", "code", project.code),
    projectField("Client", "client", project.client),
    projectField("Project name", "project_name", project.project_name),
    projectField("Email domains", "domains", project.domains, "client.com, other.com"),
    projectField("Teams channels", "channels", project.channels, "Channel names"),
    projectField("Keywords", "keywords", project.keywords, "Keywords separated by commas"),
  );
  card.append(header, fields);
  $("#project-list").append(card);
}

function renderProjects(projects) {
  $("#project-list").replaceChildren();
  projects.forEach(addProject);
}

function readProjects() {
  return $$(".project-card").map(card => {
    const project = {};
    $$("[data-key]", card).forEach(input => {
      const value = input.value.trim();
      project[input.dataset.key] = ["domains", "channels", "keywords"].includes(input.dataset.key)
        ? value.split(",").map(item => item.trim()).filter(Boolean)
        : value;
    });
    if (!project.project_name) project.project_name = null;
    return project;
  });
}

function getEntries() {
  return $$("#entries tr").map(row => ({
    project_code: $(".entry-project", row).value,
    hours: Number($(".entry-hours", row).value),
    summary: $(".entry-summary", row).value,
    confidence: Number($(".entry-confidence", row).value),
  }));
}

function updateTotals() {
  const rows = $$("#entries tr");
  let total = 0;
  let low = 0;
  for (const row of rows) {
    total += Number($(".entry-hours", row).value) || 0;
    const confidence = Number($(".entry-confidence", row).value);
    const flagged = confidence < 0.8;
    row.classList.toggle("low-confidence", flagged);
    if (flagged) low++;
  }
  $("#summary-hours").textContent = `${total.toFixed(2)}h`;
  $("#summary-low").textContent = String(low);
  $("#review-total").textContent = `Total: ${total.toFixed(2)}h`;
  $("#empty-entries").hidden = rows.length > 0;
}

function entryRow(entry) {
  const row = node("tr");
  const project = node("select", "entry-project");
  project.setAttribute("aria-label", "Project");
  const choices = registry.projects.map(item => item.code);
  if (entry.project_code && !choices.includes(entry.project_code)) choices.unshift(entry.project_code);
  choices.forEach(code => {
    const option = node("option", "", code);
    option.value = code;
    project.append(option);
  });
  project.value = entry.project_code || choices[0] || "";
  const hours = node("input", "entry-hours");
  hours.type = "number"; hours.min = "0.25"; hours.max = "24"; hours.step = "0.25"; hours.required = true; hours.value = entry.hours;
  hours.setAttribute("aria-label", "Hours");
  const summary = node("input", "entry-summary");
  summary.value = entry.summary; summary.required = true; summary.setAttribute("aria-label", "Work summary");
  const confidence = node("input", "entry-confidence");
  confidence.type = "number"; confidence.min = "0"; confidence.max = "1"; confidence.step = "0.01"; confidence.required = true;
  confidence.value = entry.confidence; confidence.setAttribute("aria-label", "Confidence");
  for (const field of [project, hours, summary, confidence]) {
    field.disabled = !editable;
    const cell = node("td"); cell.append(field); row.append(cell);
  }
  const remove = node("button", "remove", "×");
  remove.type = "button"; remove.disabled = !editable; remove.setAttribute("aria-label", "Remove entry");
  remove.addEventListener("click", () => { row.remove(); updateTotals(); });
  const cell = node("td"); cell.append(remove); row.append(cell);
  row.addEventListener("input", updateTotals);
  $("#entries").append(row);
}

function renderDay(data) {
  currentDay = data.date;
  editable = data.pending_nodes.includes("review");
  const failed = data.pending_nodes.length > 0 && !editable;
  const status = editable ? "Awaiting review" : failed ? "Needs attention" : (data.approval_status || "Completed");
  $("#summary-status").textContent = status;
  $("#day-description").textContent = editable
    ? "Edit the proposal below, then approve or reject it."
    : failed ? `Workflow paused at: ${data.pending_nodes.join(", ")}.` : "This workflow is complete.";
  $("#entries").replaceChildren();
  data.entries.forEach(entryRow);
  $("#empty-entries").textContent = "No entries in this proposal. Approving an empty proposal saves no hours.";
  for (const id of ["approve", "reject", "add-entry"]) $(`#${id}`).disabled = !editable;
  $("#retry").disabled = !failed;
  updateTotals();
}

function resetDay() {
  currentDay = null;
  editable = false;
  $("#entries").replaceChildren();
  $("#empty-entries").hidden = false;
  $("#empty-entries").textContent = "No proposal loaded yet.";
  $("#summary-status").textContent = "Not loaded";
  $("#summary-hours").textContent = "—";
  $("#summary-low").textContent = "—";
  $("#review-total").textContent = "Total: —";
  $("#day-description").textContent = "Load or generate a proposal to begin.";
  for (const id of ["approve", "reject", "add-entry", "retry"]) $(`#${id}`).disabled = true;
}

async function run(action) {
  if (busy) return;
  const day = $("#work-date").value;
  if (!day) throw new Error("Select a work date.");
  if (action !== "daily" && action !== "show" && currentDay !== day) throw new Error("Load the selected date first.");
  let body = {};
  if (action === "approve") {
    if (!confirm("Approve and save these entries?")) return;
    for (const field of $$("#entries input")) if (!field.reportValidity()) return;
    body = {entries: getEntries()};
  }
  if (action === "reject" && !confirm("Reject this proposal without saving it?")) return;
  busy = true;
  $("#load-day").disabled = true; $("#generate").disabled = true;
  try {
    const path = `/api/daily/${encodeURIComponent(day)}`;
    const data = action === "show" ? await api(path) : await api(`${path}/${action}`, {
      method: "POST", body: JSON.stringify(body),
    });
    renderDay(data);
    notice(action === "daily" ? "Proposal generated. Review it before approving." : "Workflow updated.");
  } finally {
    busy = false;
    $("#load-day").disabled = false; $("#generate").disabled = false;
  }
}

function handle(action) {
  return async () => {
    try { await action(); } catch (error) { notice(error.message, true); }
  };
}

function resetMonth() {
  $("#monthly-hours").textContent = "—";
  $("#monthly-days").textContent = "—";
  $("#monthly-pending").textContent = "—";
  $("#monthly-entries").replaceChildren();
  $("#empty-month").hidden = false;
  $("#empty-month").textContent = "No month loaded yet.";
  $("#month-message").textContent = "Load a month to see approved hours and unfinished reviews.";
  $("#export-month-button").disabled = true;
  $("#download-month").disabled = true;
}

function renderMonth(data) {
  $("#monthly-hours").textContent = `${data.total_hours.toFixed(2)}h`;
  $("#monthly-days").textContent = String(data.days_with_hours);
  $("#monthly-pending").textContent = String(data.pending_dates.length);
  const rows = $("#monthly-entries");
  rows.replaceChildren();
  for (const project of data.projects) {
    const row = node("tr");
    row.append(node("td", "", project.project_code), node("td", "", `${project.hours.toFixed(2)}h`));
    rows.append(row);
  }
  $("#empty-month").hidden = data.projects.length > 0;
  $("#empty-month").textContent = "No approved hours for this month.";
  const pending = data.pending_dates.length ? `Unfinished workflows: ${data.pending_dates.join(", ")}. ` : "";
  const missing = data.days_without_hours.length ? data.days_without_hours.join(", ") : "none";
  const output = data.output_exists ? " An export already exists and may be out of date. Generate Excel creates a new copy; Download Excel retrieves the latest copy." : "";
  $("#month-message").textContent = `${pending}Days without approved hours: ${missing}.${output}`;
  $("#export-month-button").disabled = data.pending_dates.length > 0 || !data.projects.length;
  $("#download-month").disabled = !data.output_exists;
}

async function loadMonth() {
  const month = $("#export-month").value;
  if (!month) throw new Error("Select a month.");
  const data = await api(`/api/monthly/${encodeURIComponent(month)}`);
  if ($("#export-month").value === month) renderMonth(data);
}

function renderScheduler(data) {
  $("#scheduler-badge").textContent = data.running ? "RUNNING" : "STOPPED";
  $("#scheduler-zone").textContent = data.timezone || "Not running";
  $("#scheduler-next-daily").textContent = data.next_runs.daily || "—";
  $("#scheduler-next-export").textContent = data.next_runs["month-end"] || "—";
  $("#scheduler-start").disabled = data.running;
  $("#scheduler-stop").disabled = !data.running;
  const last = Object.entries(data.last_runs);
  $("#scheduler-last").textContent = last.length
    ? last.map(([job, result]) => `${job}: ${result.status} at ${result.at}. ${result.message}`).join(" ")
    : "No jobs have run in this dashboard session.";
}

async function refreshScheduler() {
  renderScheduler(await api("/api/scheduler"));
}

async function schedulerAction(action) {
  $("#scheduler-start").disabled = true;
  $("#scheduler-stop").disabled = true;
  if (action === "stop") notice("Stopping scheduler; any running dashboard operation must finish first.");
  try {
    const result = await api(`/api/scheduler/${action}`, {method: "POST"});
    renderScheduler(result);
    notice(`Dashboard scheduler ${action === "start" ? "started" : "stopped"}.`);
  } catch (error) {
    await refreshScheduler();
    throw error;
  }
}

$("#work-date").value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
$("#export-month").value = $("#work-date").value.slice(0, 7);
$("#work-date").addEventListener("change", resetDay);
$("#export-month").addEventListener("change", resetMonth);
$("#load-month").addEventListener("click", handle(loadMonth));
$("#export-month-button").addEventListener("click", handle(async () => {
  const month = $("#export-month").value;
  if (!month) throw new Error("Select a month.");
  if (!confirm(`Generate the Excel timesheet for ${month}?`)) return;
  $("#export-month-button").disabled = true;
  try {
    const result = await api(`/api/monthly/${encodeURIComponent(month)}/export`, {method: "POST"});
    notice(`Generated ${result.filename}.`);
    if ($("#export-month").value === month) await loadMonth();
  } catch (error) {
    if ($("#export-month").value === month) await loadMonth();
    throw error;
  }
}));
$("#download-month").addEventListener("click", handle(async () => {
  const month = $("#export-month").value;
  const response = await fetch(`/api/monthly/${encodeURIComponent(month)}/download`, {
    headers: {"X-Timesheet-Token": token},
  });
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.detail || "Download failed.");
  }
  const url = URL.createObjectURL(await response.blob());
  const link = node("a");
  const filename = response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)"?/i)?.[1];
  link.href = url; link.download = filename || `timesheet-${month}.xlsx`;
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}));
$("#scheduler-start").addEventListener("click", handle(() => schedulerAction("start")));
$("#scheduler-stop").addEventListener("click", handle(() => schedulerAction("stop")));
$("#scheduler-refresh").addEventListener("click", handle(refreshScheduler));
$("#exit-application").addEventListener("click", handle(async () => {
  if (!confirm("Exit Timesheet Assistant? New operations will be blocked, the scheduler will stop, and any active operation will finish first. Unsaved form edits will be lost.")) return;
  await api("/api/lifecycle", {method: "POST"});
  exiting = true;
  $$("button, input, textarea, select").forEach(element => { element.disabled = true; });
  notice("Exit requested. Active work will finish before the application closes. You can close this tab; launch Timesheet Assistant again to reopen it.");
}));
$("#profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#save-profile");
  button.disabled = true;
  try {
    const data = Object.fromEntries(serviceFields.map(field => [field, form.elements[field].value]));
    data.azure_openai_api_key = form.elements.azure_openai_api_key.value || null;
    data.clear_api_key = form.elements.clear_api_key.checked;
    await api("/api/setup", {method: "PUT", body: JSON.stringify(data)});
    form.elements.azure_openai_api_key.value = "";
    renderProfileSetup(await api("/api/setup"));
    notice("Service settings saved with Windows protection. Sign in again if tenant or client ID changed; saving does not test connectivity.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("#template-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("#upload-template");
  button.disabled = true;
  try {
    const file = $("#template-file").files[0];
    if (!file || file.size > 10 * 1024 * 1024) throw new Error("Choose an .xlsx template at most 10 MB.");
    await api("/api/setup/template", {
      method: "POST", body: file,
      headers: {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    });
    renderTemplateStatus((await api("/api/setup")).profile_setup);
    notice("Template uploaded. Configure its Excel layout and project codes before exporting.");
  } catch (error) {
    button.disabled = false;
    notice(error.message, true);
  }
});
$("#workiq-sign-in").addEventListener("click", handle(async () => {
  const button = $("#workiq-sign-in");
  button.disabled = true;
  try {
    $("#workiq-message").textContent = "Complete Microsoft 365 sign-in in your browser. Waiting for the connection check...";
    const result = await api("/api/workiq/sign-in", {method: "POST"});
    $("#workiq-message").textContent = `Connected to Work IQ. Available tools: ${result.tools.join(", ")}. Activity operations still need configuration.`;
    notice("Microsoft 365 sign-in and Work IQ connection succeeded.");
  } catch (error) {
    $("#workiq-message").textContent = "Microsoft 365 connection failed. Try again after checking the error.";
    throw error;
  } finally {
    button.disabled = false;
  }
}));
$("#load-day").addEventListener("click", handle(() => run("show")));
$("#generate").addEventListener("click", handle(() => run("daily")));
$("#retry").addEventListener("click", handle(() => run("retry")));
$("#approve").addEventListener("click", handle(() => run("approve")));
$("#reject").addEventListener("click", handle(() => run("reject")));
$("#add-entry").addEventListener("click", () => {
  entryRow({project_code: registry.projects[0]?.code || "", hours: 0.25, summary: "", confidence: 0.5});
  updateTotals();
});
$("#add-project").addEventListener("click", () => addProject());
$("#excel-form").elements.layout_mode.addEventListener("change", updateLayoutFields);
$("#save-projects").addEventListener("click", handle(async () => {
  const projects = readProjects();
  await api("/api/registry", {method: "PUT", body: JSON.stringify({...registry, projects})});
  registry = await api("/api/registry");
  renderProjects(registry.projects);
  notice("Projects saved.");
}));
$("#excel-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const form = event.currentTarget;
    const data = {...registry.excel, ...Object.fromEntries(new FormData(form))};
    for (const field of ["project_column", "first_project_row", "last_project_row", "header_row"]) {
      data[field] = data[field] ? Number(data[field]) : null;
    }
    for (const field of ["month_header_cell", "monthly_total_column"]) data[field] = data[field] || null;
    if (typeof data.day_columns === "string") {
      data.day_columns = data.day_columns.trim() ? JSON.parse(data.day_columns) : null;
    }
    data.write_total_formula = form.elements.write_total_formula.checked;
    await api("/api/registry", {method: "PUT", body: JSON.stringify({...registry, excel: data})});
    registry = await api("/api/registry");
    renderExcel(registry.excel);
    const setup = await api("/api/setup");
    if (setup.profile_setup) renderTemplateStatus(setup.profile_setup);
    resetMonth();
    notice("Excel layout saved.");
  } catch (error) { notice(error.message, true); }
});

async function initialize() {
  try {
    const [setup, loadedRegistry] = await Promise.all([api("/api/setup"), api("/api/registry")]);
    registry = loadedRegistry;
    renderMcpStatus(setup);
    renderProfileSetup(setup);
    renderProjects(registry.projects);
    renderExcel(registry.excel);
    if (setup.profile_setup && (!setup.profile_setup.services_configured || !setup.profile_setup.template_available)) {
      switchTab("settings");
    }
  } catch (error) {
    notice(error.message, true);
  }
}

async function initializeLifecycle() {
  try {
    const lifecycle = await api("/api/lifecycle");
    $("#exit-application").hidden = !lifecycle.exit_available;
    if (lifecycle.stopping) {
      exiting = true;
      $$("button, input, textarea, select").forEach(element => { element.disabled = true; });
      notice(lifecycle.shutdown_failed ? "Clean shutdown failed. Check the application log before restarting."
        : "Application is shutting down. Active work must finish first.", lifecycle.shutdown_failed);
    }
  } catch (error) {
    notice(error.message, true);
  }
}
initialize();
initializeLifecycle();
