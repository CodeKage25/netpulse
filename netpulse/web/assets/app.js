/* Wiring: events, routing, and the poll loop. No rendering lives here. */

/* ============================== wiring ============================== */
for (const seg of document.querySelectorAll(".seg"))
  if (seg.id !== "uptime-seg")
    seg.innerHTML = RANGES.map(([label, m]) =>
      `<button data-m="${m}" class="${m === state.minutes ? "on" : ""}">${label}</button>`).join("");
document.getElementById("uptime-seg").innerHTML = UPTIME_RANGES.map(([label, days]) =>
  `<button data-days="${days}" class="${days === state.uptimeDays ? "on" : ""}">${label}</button>`).join("");

document.addEventListener("click", event => {
  const range = event.target.closest("[data-m]");
  if (range) {
    state.minutes = Number(range.dataset.m);
    for (const other of document.querySelectorAll("[data-m]"))
      other.classList.toggle("on", Number(other.dataset.m) === state.minutes);
    drawCharts();
    return;
  }
  const detailRange = event.target.closest("[data-detail-m]");
  if (detailRange) { state.detailMinutes = Number(detailRange.dataset.detailM); renderDetail(); return; }
  const usageRange = event.target.closest("[data-usage-days]");
  if (usageRange) {
    state.usageDays = Number(usageRange.dataset.usageDays);
    showUsage();
    return;
  }
  const uptimeRange = event.target.closest("[data-days]");
  if (uptimeRange) {
    state.uptimeDays = Number(uptimeRange.dataset.days);
    for (const other of document.querySelectorAll("[data-days]"))
      other.classList.toggle("on", Number(other.dataset.days) === state.uptimeDays);
    drawUptime();
    return;
  }
  const blockButton = event.target.closest("[data-block]");
  if (blockButton) { toggleBlock(blockButton); return; }
  if (event.target.closest("[data-speedtests]")) { showSpeedtests(); return; }
  const tile = event.target.closest(".tile[data-metric]");
  if (tile) { openDetail(tile.dataset.metric); return; }
  if (event.target.closest("[data-dismiss]")) closeOverlays();
});
document.addEventListener("keydown", event => { if (event.key === "Escape") closeOverlays(); });

document.getElementById("tracepath").addEventListener("click", showPath);
document.getElementById("spectrum").addEventListener("click", showSpectrum);
document.getElementById("network").addEventListener("click", showNetwork);
document.getElementById("usage").addEventListener("click", showUsage);
document.getElementById("alerts").addEventListener("click", () => {
  document.getElementById("events").scrollIntoView({ behavior: "smooth", block: "center" });
});

document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
  root.setAttribute("data-theme", next);
  try { localStorage.setItem("netpulse-theme", next); } catch {}
  refresh();
  if (state.detail && !document.getElementById("modal-root").hidden) renderDetail();
});
try {
  const saved = localStorage.getItem("netpulse-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);
} catch {}

document.getElementById("speedtest").addEventListener("click", async event => {
  const button = event.target;
  if (!confirm("A speed test moves about 30 MB of real data — many plans are metered. Run it?")) return;
  button.disabled = true; button.textContent = "Testing…";
  try {
    const result = await json(`/api/speedtest?source=${encodeURIComponent(state.primary)}`, { method: "POST" });
    button.textContent = result.error ? "Failed" : `↓ ${result.down_mbps} ↑ ${result.up_mbps} Mbps`;
    refresh();
  } catch { button.textContent = "Failed"; }
  setTimeout(() => { button.textContent = "Speed test"; button.disabled = false; }, 9000);
});

document.getElementById("settings").addEventListener("click", async () => {
  document.getElementById("drawer-root").hidden = false;
  const source = encodeURIComponent(state.primary || "");
  document.getElementById("export-csv").href =
    `/api/export?source=${source}&minutes=1440&buckets=1440&format=csv`;
  document.getElementById("export-json").href =
    `/api/export?source=${source}&minutes=1440&buckets=1440&format=json`;
  const overview = await json("/api/overview");
  document.getElementById("srclist").innerHTML = overview.sources.map(s => `
    <div class="srcrow"><span class="pulse-dot ${s.up ? "" : "down"}" style="width:8px;height:8px"></span>
      <b>${s.name}</b><span class="when">${s.kind}</span><span style="flex:1"></span>
      <span class="when">${s.texts["net.operator"] || s.texts["net.gateway"] || ""}</span></div>`).join("");
});

document.getElementById("scan").addEventListener("click", async event => {
  const button = event.target, status = document.getElementById("scanstate");
  button.disabled = true; status.textContent = "scanning…";
  try {
    const data = await json("/api/discover", { method: "POST" });
    status.textContent = data.found.length ? "" : "no router answered on the gateway or the usual addresses.";
    document.getElementById("scanresults").innerHTML = data.found.map(f => {
      const action = f.already_watched
        ? `<span style="color:var(--good);font-size:12px">watching</span>`
        : f.supported
          ? `<button class="btn primary" data-kind="${f.kind}" data-url="${f.url}">Watch it</button>`
          : `<span class="when">not supported yet</span>`;
      const help = f.supported ? "" :
        `<div style="font-size:11.5px;color:var(--text-2);margin-top:8px;line-height:1.6">
           ${f.note} Run <code>netpulse probe-router ${f.url}</code> and open an issue
           with the output — that is exactly what an adapter gets built from.</div>`;
      return `<div class="found" style="flex-wrap:wrap">
        <div><b>${f.label}</b><br><span class="when">${f.url}${f.kind ? " · " + f.kind : ""}</span></div>
        <span style="flex:1"></span>${action}${help}</div>`;
    }).join("");
  } catch { status.textContent = "scan failed"; }
  button.disabled = false;
});
document.getElementById("scanresults").addEventListener("click", async event => {
  const button = event.target.closest("button[data-kind]"); if (!button) return;
  button.disabled = true; button.textContent = "Adding…";
  await json(`/api/sources?kind=${button.dataset.kind}&url=${encodeURIComponent(button.dataset.url)}&name=${button.dataset.kind}`, { method: "POST" });
  button.textContent = "Watching ✓";
  setTimeout(refresh, 1500);
});

// Axis labels are absolutely positioned against the measured plot, so a resize or an
// orientation change has to redraw rather than reflow. Debounced: a phone rotating
// fires this many times.
let resizeTimer = 0;
const redraw = () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!firstPaintDone) return;
    drawCharts();
    if (state.detail && !document.getElementById("modal-root").hidden) renderDetail();
  }, 180);
};
window.addEventListener("resize", redraw);
window.addEventListener("orientationchange", redraw);

const OVERLAYS = { spectrum: showSpectrum, path: showPath, speedtests: showSpeedtests,
                   network: showNetwork, usage: showUsage };
refresh().then(() => {
  const target = location.hash.slice(1);
  if (!target) return;
  // #spectrum, #path and #speedtests open their view; anything else is a metric.
  (OVERLAYS[target] || (() => openDetail(target)))();
});
setInterval(refresh, 15000);
try {
  const stream = new EventSource("/api/stream");
  let last = 0;
  stream.onmessage = () => { if (firstPaintDone && Date.now() - last > 4000) { last = Date.now(); refresh(); } };
} catch {}
