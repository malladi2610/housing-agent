"use strict";
document.addEventListener("click", async (event) => {
  const refresh = event.target.closest("[data-refresh]");
  if (refresh) window.location.reload();
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const field = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(field.value);
    button.textContent = "Copied ✓";
    setTimeout(() => { button.textContent = "Copy message"; }, 1800);
  } catch {
    field.focus(); field.select();
    button.textContent = "Select text and copy";
  }
});
document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
let dirty = false;
document.addEventListener("input", () => { dirty = true; });
setInterval(async () => {
  try {
    const response = await fetch("/api/status", {credentials: "same-origin"});
    if (!response.ok) return;
    const status = await response.json();
    const worker = status.connectors.find((connection) => connection.name === "worker");
    const recent = worker && Date.now() - new Date(worker.last_attempt).getTime() < 30000;
    const online = recent && !status.profile_paused && !status.search_complete;
    const badge = document.getElementById("worker-badge");
    const label = status.search_complete ? "Search complete" : status.profile_paused ? "Search paused" : online ? "Worker online" : "Worker offline";
    badge.className = `pill ${online ? "good" : "muted"}`;
    badge.replaceChildren();
    const dot = document.createElement("span"); dot.className = "dot";
    badge.append(dot, document.createTextNode(label));
    if (["/", "/jobs", "/android"].includes(window.location.pathname) && !dirty && document.visibilityState === "visible") {
      const jobs = await fetch("/api/jobs", {credentials: "same-origin"}).then((r) => r.json());
      const signature = JSON.stringify(jobs.map((job) => [job.id, job.status]));
      const previous = sessionStorage.getItem("homeward-jobs");
      sessionStorage.setItem("homeward-jobs", signature);
      if (previous && previous !== signature) window.location.reload();
    }
  } catch { /* The visible heartbeat expires; reconnect on the next interval. */ }
}, 12000);

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-node-selector]");
  if (!button) return;
  const role = document.getElementById("selector-target").value;
  const form = document.getElementById("android-config");
  form.elements[`selector_${role}_using`].value = button.dataset.nodeUsing;
  form.elements[`selector_${role}_value`].value = button.dataset.nodeSelector;
  dirty = true;
  button.textContent = `Selected for ${role}`;
});
