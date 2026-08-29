"use strict";
const RANGES = [["15m", 15], ["1h", 60], ["6h", 360], ["24h", 1440], ["7d", 10080]];
const state = {
  owner: {}, primary: null, minutes: 60, detail: null, detailMinutes: 60, uptimeDays: 7,
};
const UPTIME_RANGES = [["24h", 1], ["7d", 7], ["30d", 30]];

/* A router and a probe measure different halves of the same connection: the router
   knows signal, bands and data, and cannot see past its own WAN port; the probe knows
   what the internet actually feels like, and knows nothing about the radio. Making
   someone choose between them is what left the page looking empty, so each metric is
   resolved to whichever source actually reports it and the dashboard shows one
   connection rather than two partial ones. */
function resolveOwners(overview) {
  const owner = {};
  for (const source of overview.sources)
    for (const metric of Object.keys(source.latest))
      // First writer wins, and sources arrive router-first, so the router keeps the
      // radio metrics; latency and loss only ever come from the probe anyway.
      if (!(metric in owner)) owner[metric] = source.name;
  return owner;
}

const fmt = {
  ms: v => v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1) + "s" : Math.round(v) + "ms",
  mbps: v => v == null ? "–" : (v * 8 / 1e6) >= 100 ? String(Math.round(v * 8 / 1e6)) : (v * 8 / 1e6).toFixed(1),
  bytes: v => {
    if (v == null) return "–";
    const units = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return v.toFixed(v >= 100 ? 0 : 1) + " " + units[i];
  },
  axis: v => Math.abs(v) >= 1000 ? (v / 1000).toFixed(0) + "k"
    : Math.abs(v) >= 10 ? String(Math.round(v)) : String(Math.round(v * 10) / 10),
  when: iso => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
  hm: iso => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }),
  clock: iso => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
async function json(url, opts) { const r = await fetch(url, opts); if (!r.ok) throw new Error(url); return r.json(); }

/* ==========================================================================
   One spec per metric drives the tile, the chart and the detail view, so a
   new metric is a row here rather than three places to keep in agreement.
   ========================================================================== */
const METRICS = {
  "traffic.down_bytes_s": {
    label: "Download", unit: "Mbps", color: "--down", fill: "--down-fill",
    tile: m => fmt.mbps(m), caption: "current traffic",
    toChart: v => v * 8 / 1e6, chartFmt: v => v.toFixed(1) + " Mbps", axisFmt: fmt.axis,
    explain: ["What is throughput?",
      "How much data is crossing the link right now, read from the router's own counters " +
      "every few seconds. It measures what your devices are actually using, not how fast " +
      "the connection could go — an idle link reads near zero however fast it is. For " +
      "capacity, run a speed test."],
  },
  "traffic.up_bytes_s": {
    label: "Upload", unit: "Mbps", color: "--up", fill: "--up-fill",
    tile: m => fmt.mbps(m), caption: "current traffic",
    toChart: v => v * 8 / 1e6, chartFmt: v => v.toFixed(1) + " Mbps", axisFmt: fmt.axis,
    explain: ["What is upload?",
      "Data leaving your network — video calls, backups, sending photos. It is usually a " +
      "fraction of download capacity on mobile networks, and it is what suffers first when " +
      "a cell is congested."],
  },
  "latency.internet_ms": {
    label: "Latency", unit: "ms", color: "--mono", fill: "--mono-fill",
    tile: m => Math.round(m), caption: "to the internet, live",
    chartFmt: fmt.ms, axisFmt: v => Math.round(v), floor: 0,
    explain: ["What is latency?",
      "How long a small packet takes to reach the internet and come back. Video calls and " +
      "games feel the spikes far more than the average, so the chart keeps the worst value " +
      "in each bucket rather than smoothing it away — a spike averaged into a minute would " +
      "read as perfectly fine."],
  },
  "signal.rsrp_dbm": {
    label: "Signal", unit: "dBm", color: "--mono", fill: "--mono-fill",
    tile: m => Math.round(m), caption: "RSRP at the router",
    chartFmt: v => v.toFixed(1) + " dBm", axisFmt: v => Math.round(v), floor: -120,
    explain: ["What is RSRP?",
      "The raw strength of the tower's signal reaching your router, in dBm — a negative " +
      "number where closer to zero is stronger. Better than −90 is strong, −90 to −105 is " +
      "usable, below −110 will drop. Moving the router near a window, or higher, usually " +
      "moves this number more than anything else you can do."],
  },
  "signal.sinr_db": {
    label: "Quality", unit: "dB", color: "--accent", fill: "--accent-fill",
    tile: m => m.toFixed(0), caption: "SINR — higher is cleaner",
    chartFmt: v => v.toFixed(1) + " dB", axisFmt: v => Math.round(v), floor: -5,
    explain: ["What is SINR?",
      "How clean the signal is against noise and neighbouring cells. Strength without " +
      "quality still gives you a slow connection, which is why a full-bars router can " +
      "crawl at busy hours. Above 20 dB is excellent, 13–20 good, below 5 dB is where " +
      "throughput collapses even on a strong signal."],
  },
  "signal.rsrp_5g_dbm": {
    label: "5G signal", unit: "dBm", color: "--down", fill: "--down-fill",
    tile: m => Math.round(m), caption: "RSRP on the 5G carrier",
    chartFmt: v => v.toFixed(1) + " dBm", axisFmt: v => Math.round(v), floor: -120,
    explain: ["What is the 5G carrier?",
      "On 5G non-standalone your router holds an LTE anchor and a 5G carrier at the " +
      "same time, and the 5G leg does most of the carrying. Its strength can differ " +
      "sharply from the anchor's, which is why both are tracked separately — a strong " +
      "anchor with a weak 5G leg still feels slow."],
  },
  "signal.sinr_5g_db": {
    label: "5G quality", unit: "dB", color: "--accent", fill: "--accent-fill",
    tile: m => m.toFixed(0), caption: "SINR on the 5G carrier",
    chartFmt: v => v.toFixed(1) + " dB", axisFmt: v => Math.round(v), floor: -5,
    explain: ["Why does 5G quality matter more than strength?",
      "The 5G carrier uses wide channels at high frequency, where interference costs " +
      "more than distance. A low SINR here collapses throughput even while the signal " +
      "bars stay full, and it is the usual explanation for a fast connection that " +
      "crawls at busy hours."],
  },
  "loss.pct": {
    label: "Ping success", unit: "%", color: "--good", fill: "--mono-fill",
    tile: m => (100 - m).toFixed(0), caption: "last probe round",
    chartFmt: v => v.toFixed(1) + "% lost", axisFmt: v => Math.round(v), floor: 0,
    explain: ["What is packet loss?",
      "The share of test packets that never came back. Steady loss above about 2% breaks " +
      "calls and stalls downloads even when latency looks fine, because every lost packet " +
      "has to be noticed and sent again."],
  },
};

/* ============================== charts ============================== */
function runs(points) {
  const out = []; let current = null;
  points.forEach((v, i) => {
    if (v == null) { current = null; return; }
    if (!current) { current = []; out.push(current); }
    current.push(i);
  });
  return out;
}

/* An axis reading 9.8 / 4.9 / 0 is a scale nobody can hold in their head. Steps snap to
   a round number, and the ceiling is a whole multiple of the step, so the top gridline
   is always the ceiling rather than a stray line short of it. */
const NICE_STEPS = [1, 1.5, 2, 2.5, 4, 5, 8, 10];

function niceStep(rough) {
  if (!(rough > 0)) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const scaled = rough / magnitude;
  return magnitude * (NICE_STEPS.find(step => scaled <= step + 1e-9) ?? 10);
}

function axis(lo, hi, divisions = 3) {
  // A floor is honoured (RSRP starts at -120, not 0) but never at the cost of a
  // readable scale, so the floor is snapped down to a step boundary too.
  const step = niceStep((hi - lo) / divisions);
  const base = Math.floor(lo / step) * step;
  const top = base + step * Math.ceil((hi - base) / step || 1);
  const ticks = [];
  for (let value = base; value <= top + step * 1e-6; value += step) ticks.push(value);
  return { base, top, ticks };
}

function chart(el, seriesList, opts) {
  const { times, bands = [], format = fmt.axis, tip = fmt.ms, height = 190, floor = null } = opts;
  const W = 1000, H = height, pad = { l: 4, r: 4, t: 12, b: 8 };
  const n = Math.max(1, times.length - 1);
  const all = seriesList.flatMap(s => s.points).filter(v => v != null);
  const dataHi = all.length ? Math.max(...all) : 1;
  const dataLo = all.length ? Math.min(...all) : 0;
  const rawLo = floor != null ? Math.min(floor, dataLo) : (dataLo >= 0 ? 0 : dataLo * 1.08);
  const rawHi = dataHi + (dataHi - rawLo) * 0.08 || 1;
  const { base: lo, top, ticks: yTicks } = axis(rawLo, rawHi);
  const x = i => pad.l + (i / n) * (W - pad.l - pad.r);
  const y = v => pad.t + (1 - (v - lo) / (top - lo)) * (H - pad.t - pad.b);
  const baseline = H - pad.b;

  let svg = `<svg class="plot" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">`;
  for (const [a, b] of bands)
    svg += `<rect x="${x(a).toFixed(1)}" y="${pad.t}" width="${Math.max(3, x(b) - x(a)).toFixed(1)}" height="${H - pad.t - pad.b}" fill="var(--band)"/>`;
  // A bucket nobody recorded is shaded: the gap is the finding, not an inconvenience
  // to be smoothed over. Every series must be silent for it to count as one.
  for (let i = 0; i < times.length; i++)
    if (seriesList.every(series => series.points[i] == null))
      svg += `<rect x="${x(Math.max(0, i - 0.5)).toFixed(1)}" y="${pad.t}" width="${((W - pad.l - pad.r) / n).toFixed(1)}" height="${H - pad.t - pad.b}" fill="var(--gapfill)"/>`;
  let ylabs = "";
  for (const value of yTicks) {
    const yy = y(value);
    svg += `<line x1="${pad.l}" y1="${yy.toFixed(1)}" x2="${W - pad.r}" y2="${yy.toFixed(1)}" stroke="var(--grid)" stroke-width="1"/>`;
    ylabs += `<span class="ylab" style="top:${(yy / H) * 100}%">${format(value)}</span>`;
  }
  for (const s of seriesList) {
    for (const run of runs(s.points)) {
      if (run.length === 1) {
        svg += `<circle cx="${x(run[0]).toFixed(1)}" cy="${y(s.points[run[0]]).toFixed(1)}" r="2.5" fill="${s.color}"/>`;
        continue;
      }
      let line = "", area = `M${x(run[0]).toFixed(1)} ${baseline.toFixed(1)}`;
      run.forEach((i, k) => {
        line += `${k === 0 ? "M" : "L"}${x(i).toFixed(1)} ${y(s.points[i]).toFixed(1)}`;
        area += `L${x(i).toFixed(1)} ${y(s.points[i]).toFixed(1)}`;
      });
      area += `L${x(run[run.length - 1]).toFixed(1)} ${baseline.toFixed(1)}Z`;
      if (s.fill) svg += `<path d="${area}" fill="${s.fill}"/>`;
      svg += `<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`;
    }
  }
  svg += `<line class="cross" y1="${pad.t}" y2="${H - pad.b}" stroke="var(--text-3)" stroke-width="1" opacity="0"/></svg>`;

  let xlabs = "";
  const ticks = Math.min(5, times.length);
  for (let t = 0; t < ticks; t++) {
    const i = Math.round((t / Math.max(1, ticks - 1)) * (times.length - 1));
    // Edge labels clamp inward so the first cannot sit under the y-axis column
    // and the last cannot spill past the plot.
    const shift = t === 0 ? "0" : t === ticks - 1 ? "-100%" : "-50%";
    if (times[i]) xlabs += `<span class="xlab" data-frac="${(x(i) / W).toFixed(4)}" style="transform:translateX(${shift})">${fmt.hm(times[i])}</span>`;
  }

  el.innerHTML = svg + ylabs + xlabs + `<div class="tooltip"></div>`;
  const node = el.querySelector("svg.plot");
  const place = () => {
    const plot = node.getBoundingClientRect(), box = el.getBoundingClientRect();
    for (const lab of el.querySelectorAll(".xlab"))
      lab.style.left = (plot.left - box.left + Number(lab.dataset.frac) * plot.width) + "px";
  };
  place();
  requestAnimationFrame(place);

  const tt = el.querySelector(".tooltip"), cross = el.querySelector(".cross");
  node.addEventListener("mousemove", event => {
    const rect = node.getBoundingClientRect();
    const i = Math.round((((event.clientX - rect.left) / rect.width * W) - pad.l) / (W - pad.l - pad.r) * n);
    if (i < 0 || i >= times.length) { tt.style.display = "none"; cross.setAttribute("opacity", 0); return; }
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", .45);
    tt.innerHTML = `<div class="t">${fmt.clock(times[i])}</div>` + seriesList.map(s => {
      const v = s.points[i];
      return `<div><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${s.color};margin-right:6px"></span>${s.name}: <b>${v == null ? "no data" : tip(v)}</b></div>`;
    }).join("");
    tt.style.display = "block";
    tt.style.left = Math.min(rect.width - tt.offsetWidth - 6, Math.max(6, (x(i) / W) * rect.width + 14)) + "px";
    tt.style.top = "8px";
  });
  node.addEventListener("mouseleave", () => { tt.style.display = "none"; cross.setAttribute("opacity", 0); });
}

function histogram(el, bins, color, format, overflowing) {
  if (!bins.length) { el.innerHTML = `<div class="empty">Not enough samples yet.</div>`; return; }
  const W = 1000, H = 130, gap = 2;
  const peak = Math.max(...bins.map(b => b.count)) || 1;
  const total = bins.reduce((sum, b) => sum + b.count, 0) || 1;
  const width = W / bins.length;
  let svg = `<svg class="plot" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px">`;
  bins.forEach((bin, i) => {
    const h = (bin.count / peak) * (H - 6);
    // 4px rounded data-ends anchored to the baseline; a 2px gap keeps bars distinct.
    svg += `<rect x="${(i * width + gap / 2).toFixed(1)}" y="${(H - h).toFixed(1)}" ` +
           `width="${Math.max(1, width - gap).toFixed(1)}" height="${h.toFixed(1)}" ` +
           `rx="2" fill="${color}" opacity="${0.45 + 0.55 * (bin.count / peak)}"><title>` +
           `${format(bin.lo)} – ${format(bin.hi)}: ${(100 * bin.count / total).toFixed(1)}%</title></rect>`;
  });
  svg += `</svg>`;
  const ceiling = format(bins[bins.length - 1].hi) + (overflowing ? "+" : "");
  el.innerHTML = svg +
    `<div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--text-3);margin-top:3px">
       <span>${format(bins[0].lo)}</span>
       <span>${(100 * peak / total).toFixed(1)}% in the tallest bin</span>
       <span>${ceiling}</span></div>`;
}

function spark(points, color) {
  const vals = points.filter(v => v != null);
  if (vals.length < 2) return "";
  const W = 100, H = 34, hi = Math.max(...vals), lo = Math.min(...vals), span = hi - lo || 1;
  const x = i => (i / (points.length - 1)) * W;
  const y = v => 3 + (1 - (v - lo) / span) * (H - 6);
  let d = "";
  for (const run of runs(points))
    run.forEach((i, k) => { d += `${k === 0 ? "M" : "L"}${x(i).toFixed(1)} ${y(points[i]).toFixed(1)}`; });
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="${color}" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

/* ============================== dashboard ============================== */
let firstPaintDone = false, lastOverview = null;

async function refresh() {
  const overview = await json("/api/overview");
  lastOverview = overview;
  state.owner = resolveOwners(overview);
  // The router describes the connection; the probe describes the internet beyond it.
  const router = overview.sources.find(s => s.kind !== "probe");
  state.primary = (router || overview.sources[0])?.name;
  const src = overview.sources.find(s => s.name === state.primary);
  if (!src) return;

  // Merged reading: every metric from whichever source reports it.
  const merged = { latest: {}, sparklines: {}, texts: {} };
  for (const source of overview.sources)
    for (const field of ["latest", "sparklines", "texts"])
      for (const [key, value] of Object.entries(source[field]))
        if (!(key in merged[field])) merged[field][key] = value;

  // Offline anywhere is offline: a router with a live radio and no route to the
  // internet is not a working connection, however healthy its own status page looks.
  const up = overview.sources.every(s => s.up);
  document.getElementById("dot").className = "pulse-dot" + (up ? "" : " down");
  const parts = [`<b>${up ? "online" : "offline"}</b>`];
  src.latest = merged.latest; src.sparklines = merged.sparklines;
  src.texts = { ...merged.texts, ...src.texts };
  if (src.texts["net.operator"]) parts.push(src.texts["net.operator"]);
  if (src.texts["net.type"]) parts.push(`<span class="tag">${src.texts["net.type"]}</span>`);
  if (src.texts["signal.band"]) parts.push(`<span class="tag">${src.texts["signal.band"]}</span>`);
  if (src.latest["router.uptime_s"] != null) {
    const hours = Math.floor(src.latest["router.uptime_s"] / 3600);
    const mins = Math.floor((src.latest["router.uptime_s"] % 3600) / 60);
    parts.push(`router up ${hours}h ${mins}m`);
  }
  if (src.uptime_24h != null) parts.push(`link up ${(src.uptime_24h * 100).toFixed(1)}% · 24h`);
  document.getElementById("headmeta").innerHTML = parts.join(" · ");

  renderNotices(overview);
  renderTiles(src);

  const facts = [];
  for (const [key, label] of [["net.operator", "Operator"], ["net.type", "Network"],
      ["signal.band", "Band"], ["signal.cell_id", "Cell"], ["net.gateway", "Gateway"]])
    if (src.texts[key]) facts.push(`<span class="k">${label}</span><span class="v">${src.texts[key]}</span>`);
  facts.push(`<span class="k">Measured by</span><span class="v">${
    overview.sources.map(s => s.kind).join(" + ")}</span>`);
  facts.push(`<span class="k">Recorded</span><span class="v">${
    (Math.max(...overview.sources.map(s => s.coverage)) * 100).toFixed(0)}% of the last hour</span>`);
  document.getElementById("facts").innerHTML = facts.join("");

  await Promise.all([drawCharts(), drawQuality(), drawAllowance(), drawUptime(),
                     drawInsights(), drawEvents(), drawDevices()]);
  firstPaintDone = true;
}

function renderTiles(src) {
  const m = src.latest, sp = src.sparklines;
  let html = "";
  for (const [key, spec] of Object.entries(METRICS)) {
    if (m[key] == null) continue;
    html += `<button class="tile" data-metric="${key}"><span class="chev">›</span>
      <div class="label">${spec.label}</div>
      <div class="row"><span class="value">${spec.tile(m[key])}<span class="unit">${spec.unit}</span></span>
      <span class="spark">${sp[key] ? spark(sp[key], css(spec.color)) : ""}</span></div>
      <div class="caption">${spec.caption}</div></button>`;
  }
  // Some firmware reports a single monthly total, others a down/up pair. Adding a
  // total to an upload figure would double-count, so the total wins where it exists.
  const monthly = m["data.month_total_bytes"] != null
    ? m["data.month_total_bytes"]
    : m["data.month_down_bytes"] != null
      ? m["data.month_down_bytes"] + (m["data.month_up_bytes"] || 0)
      : null;
  if (monthly != null)
    html += `<div class="tile" style="cursor:default"><div class="label">Data this month</div>
      <div class="row"><span class="value">${fmt.bytes(monthly)}</span></div>
      <div class="caption">counted by the router</div></div>`;
  if (m["speedtest.down_bytes_s"] != null)
    html += `<div class="tile" style="cursor:default"><div class="label">Speed test</div>
      <div class="row"><span class="value">${fmt.mbps(m["speedtest.down_bytes_s"])}<span class="unit">Mbps ↓</span></span></div>
      <div class="caption">↑ ${fmt.mbps(m["speedtest.up_bytes_s"])} Mbps — on demand</div></div>`;
  document.getElementById("tiles").innerHTML = html;
}

/* A dashboard with two tiles should say why, not just look empty. */
function renderNotices(overview) {
  const box = document.getElementById("notices");
  // The test is what is missing, not which adapters are configured: a router that is
  // present but reporting nothing leaves exactly the same hole for the reader.
  if ("signal.rsrp_dbm" in state.owner) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="notice">
    <div style="flex:1">
      <h3>Only this machine's connection is being measured</h3>
      <p>Latency and packet loss come from probing the internet directly. Signal strength,
      data usage and connected devices have to be read from the router itself — scan for
      it, and those tiles appear. If your router is found but not yet supported, run
      <code>netpulse probe-router http://192.168.0.1</code> and open an issue with the output.</p>
    </div>
    <button class="btn primary" onclick="document.getElementById('settings').click()">Find my router</button>
  </div>`;
}

async function drawCharts() {
  const q = metric => {
    const source = state.owner[metric] || state.primary;
    return json(`/api/history?source=${encodeURIComponent(source)}&metric=${metric}` +
                `&minutes=${state.minutes}&buckets=120`);
  };
  const [latency, downH, upH, rsrp, sinr, events] = await Promise.all([
    q("latency.internet_ms"), q("traffic.down_bytes_s"), q("traffic.up_bytes_s"),
    q("signal.rsrp_dbm"), q("signal.sinr_db"), json(`/api/events?minutes=${state.minutes}`),
  ]);
  const times = latency.times;
  const bands = events.events
    .filter(e => e.kind === "outage")
    .map(e => {
      const a = times.findIndex(t => t >= e.started_at);
      const b = e.ended_at ? times.findIndex(t => t >= e.ended_at) : times.length - 1;
      return a >= 0 ? [a, b < 0 ? times.length - 1 : b] : null;
    }).filter(Boolean);

  chart(document.getElementById("throughput"),
    [{ name: "download", color: css("--down"), fill: css("--down-fill"), points: downH.points.map(v => v == null ? null : v * 8 / 1e6) },
     { name: "upload", color: css("--up"), fill: css("--up-fill"), points: upH.points.map(v => v == null ? null : v * 8 / 1e6) }],
    { times: downH.times, height: 210, format: fmt.axis, tip: v => v.toFixed(1) + " Mbps" });

  chart(document.getElementById("latency"),
    [{ name: "latency", color: css("--mono"), fill: css("--mono-fill"), points: latency.points }],
    { times, bands, height: 190, format: v => Math.round(v), tip: fmt.ms, floor: 0 });
  document.getElementById("latency-note").textContent =
    `Recorded ${(latency.coverage * 100).toFixed(0)}% of this window — shaded stretches were not sampled, and nothing is invented across them.`;

  const hasSignal = rsrp.points.some(v => v != null);
  document.getElementById("signal-panel").hidden = !hasSignal;
  if (hasSignal) {
    // On 5G non-standalone both carriers are live at once, so both get drawn — the
    // anchor alone would hide the leg that is actually carrying the traffic.
    const [rsrp5g, sinr5g] = await Promise.all([q("signal.rsrp_5g_dbm"), q("signal.sinr_5g_db")]);
    const dual = rsrp5g.points.some(v => v != null);
    document.getElementById("signal-legend").innerHTML = dual
      ? `<span><span class="dot" style="background:var(--mono)"></span>LTE anchor</span>
         <span><span class="dot" style="background:var(--down)"></span>5G carrier</span>`
      : `<span><span class="dot" style="background:var(--mono)"></span>serving cell</span>`;
    // Two filled areas at similar values stack into an unreadable block, so the second
    // carrier is a line over the first one's fill — Dishylink's two-series treatment.
    const pair = (base, extra) => extra
      ? [base, { name: "5G", color: css("--down"), fill: null, points: extra }]
      : [base];
    chart(document.getElementById("rsrp"),
      pair({ name: "LTE", color: css("--mono"), fill: css("--mono-fill"), points: rsrp.points },
           dual ? rsrp5g.points : null),
      { times: rsrp.times, height: 150, floor: -120, format: v => Math.round(v), tip: v => v.toFixed(1) + " dBm" });
    chart(document.getElementById("sinr"),
      pair({ name: "LTE", color: css("--mono"), fill: css("--mono-fill"), points: sinr.points },
           dual ? sinr5g.points : null),
      { times: sinr.times, height: 150, floor: -5, format: v => Math.round(v), tip: v => v.toFixed(1) + " dB" });
  }
}

async function drawQuality() {
  // Graded on whoever measures internet latency — the router cannot see past its WAN.
  const source = state.owner["latency.internet_ms"] || state.primary;
  const { quality } = await json(`/api/quality?source=${encodeURIComponent(source)}`);
  if (!quality) return;
  document.getElementById("quality").innerHTML = `
    <div class="grade-row">
      <div class="grade ${quality.grade}">${quality.grade}</div>
      <div class="grade-detail">
        score <b>${quality.score}</b>/100 · median <b>${quality.p50_ms}ms</b> · p95 <b>${quality.p95_ms}ms</b><br>
        jitter <b>${quality.jitter_ms}ms</b> · loss <b>${quality.loss_pct}%</b>
      </div>
    </div>`;
}

async function drawAllowance() {
  // Owned by whichever source publishes an odometer — only a router has one.
  const source = state.owner["data.month_total_bytes"] || state.owner["data.month_down_bytes"];
  const panel = document.getElementById("allowance-panel");
  if (!source) { panel.hidden = true; return; }
  const { allowance } = await json(`/api/allowance?source=${encodeURIComponent(source)}`);
  if (!allowance) { panel.hidden = true; return; }
  panel.hidden = false;

  const used = fmt.bytes(allowance.used_bytes);
  const elapsed = Math.min(1, allowance.days_elapsed / allowance.days_total);
  const left = allowance.days_total - allowance.days_elapsed;
  document.getElementById("cycle-hint").textContent =
    `${left < 1 ? "last day" : Math.ceil(left) + " days left"} · resets ${
      new Date(allowance.cycle_end + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })}`;

  if (allowance.limit_bytes == null) {
    // No plan configured: report the meter, do not invent a limit to judge it against.
    document.getElementById("allowance").innerHTML = `
      <div class="allow-top"><span class="big">${used}</span>
        <span class="of">used this cycle</span></div>
      <div class="verdict" style="color:var(--text-3)">
        Add <code>limit_gb</code> under <code>[plan]</code> in your config to track it
        against your allowance.</div>`;
    return;
  }

  const fraction = allowance.fraction;
  const tone = fraction >= 1 ? "over" : fraction >= 0.8 ? "warn" : "";
  const verdict = allowance.on_track === null ? ""
    : allowance.exhausted_on
      ? `<div class="verdict bad">At this rate it runs out on ${
          new Date(allowance.exhausted_on + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" })
        } — ${fmt.bytes(allowance.projected_bytes)} projected for the cycle.</div>`
      : `<div class="verdict good">On track — ${fmt.bytes(allowance.projected_bytes)} projected against ${fmt.bytes(allowance.limit_bytes)}.</div>`;

  document.getElementById("allowance").innerHTML = `
    <div class="allow-top"><span class="big">${used}</span>
      <span class="of">of ${fmt.bytes(allowance.limit_bytes)} · ${(fraction * 100).toFixed(0)}%</span></div>
    <div class="meter">
      <i class="${tone}" style="width:${Math.min(100, fraction * 100).toFixed(1)}%"></i>
      ${fraction < elapsed ? `<span style="flex:1"></span>` : ""}
    </div>
    <div style="font-size:11px;color:var(--text-3)">
      ${(elapsed * 100).toFixed(0)}% of the cycle elapsed${
        allowance.rate_per_day ? ` · ${fmt.bytes(allowance.rate_per_day)}/day so far` : ""}</div>
    ${verdict}`;
}

async function drawUptime() {
  const source = state.owner["up"] || state.primary;
  const report = await json(
    `/api/uptime?source=${encodeURIComponent(source)}&days=${state.uptimeDays}`);
  const box = document.getElementById("uptime");
  if (report.uptime == null) {
    // An empty week is not a flawless week, and must not be shown as one.
    box.innerHTML = `<div class="empty">Nothing recorded over this period.</div>`;
    return;
  }
  const hours = Math.floor(report.downtime_seconds / 3600);
  const mins = Math.round((report.downtime_seconds % 3600) / 60);
  const down = report.downtime_seconds === 0 ? "none"
    : hours ? `${hours}h ${mins}m` : `${mins}m`;
  box.innerHTML = `
    <div class="up-figures">
      <div><div class="n">${(report.uptime * 100).toFixed(2)}<span style="font-size:13px">%</span></div>
        <div class="l">of recorded polls were up</div></div>
      <div><div class="n" style="font-size:20px;color:var(--text-2)">${
        // Rounding a real 0.4% to "0%" reads as "nothing was recorded", which is a
        // different and wrong claim — the figure above it came from somewhere.
        report.coverage < 0.1 ? (report.coverage * 100).toFixed(1) : (report.coverage * 100).toFixed(0)
      }%</div>
        <div class="l">of the period recorded</div></div>
    </div>
    <div style="font-size:11.5px;color:var(--text-3);margin-top:9px;line-height:1.6">
      ${report.outages} outage${report.outages === 1 ? "" : "s"} · ${down} down · longest
      ${Math.round(report.longest_outage_seconds / 60)}m.
      ${report.coverage < 0.9 ? "The two figures are separate on purpose: an uptime "
        + "measured over part of a period is not a claim about the whole of it." : ""}
    </div>`;
}

async function drawInsights() {
  // Every source gets diagnosed: the router explains the radio, the probe explains the
  // path beyond it, and a fault in either is a fault in the connection.
  const names = [...new Set(Object.values(state.owner))];
  const results = await Promise.all(names.map(name =>
    json(`/api/insights?source=${encodeURIComponent(name)}`).catch(() => ({ insights: [] }))));
  const rank = { critical: 0, warning: 1, info: 2 };
  const seen = new Set();
  const insights = results.flatMap(r => r.insights)
    .filter(i => !seen.has(i.title) && seen.add(i.title))
    .sort((a, b) => rank[a.severity] - rank[b.severity]);
  const color = { critical: "var(--critical)", warning: "var(--serious)", info: "var(--good)" };
  document.getElementById("insights").innerHTML = insights.length
    ? insights.map(i => `<div class="insight"><div class="bar" style="background:${color[i.severity]}"></div>
        <div><div class="title">${i.title}</div><div class="detail">${i.detail}</div></div></div>`).join("")
    : `<div class="empty">Nothing to flag — the connection looks healthy.</div>`;
}

async function drawEvents() {
  const data = await json("/api/events?minutes=10080");
  const active = data.events.filter(e => !e.ended_at).length;
  const badge = document.getElementById("alert-count");
  badge.textContent = active || "";
  badge.className = active ? "on" : "";
  document.getElementById("evcount").textContent = data.events.length ? `${data.events.length} in 7d` : "";
  document.getElementById("events").innerHTML = data.events.length
    ? data.events.slice(0, 10).map(e => {
        const mins = e.ended_at ? Math.max(1, Math.round((new Date(e.ended_at) - new Date(e.started_at)) / 60000)) : null;
        return `<div class="row-item"><span class="when" style="min-width:74px">${fmt.when(e.started_at)}</span>
          <span class="sev ${e.severity}"></span><span>${e.kind} · ${e.source}</span>
          ${e.ended_at ? `<span class="dur">${mins}m</span>` : `<span class="live">ongoing</span>`}</div>`;
      }).join("")
    : `<div class="empty">No outages recorded.</div>`;
}

async function drawDevices() {
  const data = await json(`/api/devices?source=${encodeURIComponent(state.primary)}&hours=24`);
  document.getElementById("devices-panel").hidden = data.devices.length === 0;
  document.getElementById("devcount").textContent = `${data.devices.length} · 24h`;
  document.getElementById("devices").innerHTML = data.devices.map(d =>
    `<div class="row-item"><span style="font-weight:600">${d.name || "unnamed"}</span>
     <span class="when">${d.ip}</span><span style="flex:1"></span>
     <span class="when">${fmt.when(d.last_seen)}</span></div>`).join("");
}

/* ============================== detail view ============================== */
async function openDetail(metric) {
  if (!METRICS[metric]) return;
  state.detail = metric;
  state.detailMinutes = state.minutes;
  document.getElementById("modal-root").hidden = false;
  // The open view lives in the URL, so a reload lands where you were and a link to
  // "the latency page" is a link someone can actually send.
  if (location.hash.slice(1) !== metric) history.replaceState(null, "", "#" + metric);
  await renderDetail();
}

function closeOverlays() {
  document.getElementById("modal-root").hidden = true;
  document.getElementById("drawer-root").hidden = true;
  state.detail = null;
  if (location.hash) history.replaceState(null, "", location.pathname);
}

async function renderDetail() {
  const metric = state.detail, spec = METRICS[metric];
  const minutes = state.detailMinutes;
  const source = encodeURIComponent(state.owner[metric] || state.primary);
  const [series, dist] = await Promise.all([
    json(`/api/history?source=${source}&metric=${metric}&minutes=${minutes}&buckets=140`),
    json(`/api/distribution?source=${source}&metric=${metric}&minutes=${minutes}`),
  ]);
  const toChart = spec.toChart || (v => v);
  const live = lastOverview?.sources.find(s => s.name === state.primary)?.latest?.[metric];
  // Best and worst come from raw samples, never from the plotted series: latency
  // buckets keep the worst value in each, so the smallest of those maxima is not the
  // best reading — it is the best bad minute, which is a different and wrong number.
  const heroes = [
    ["Current", live == null ? "–" : spec.tile(live)],
    ["Average", dist.mean == null ? "–" : spec.tile(dist.mean)],
    ["Best", dist.min == null ? "–" : spec.tile(dist.min)],
    ["Worst", dist.max == null ? "–" : spec.tile(dist.max)],
  ];

  document.getElementById("modal").innerHTML = `
    <div class="modal-head"><h2>${spec.label}</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="heroes">${heroes.map(([label, value]) =>
      `<div class="hero"><div class="n">${value}<span>${spec.unit}</span></div>
       <div class="l">${label}</div></div>`).join("")}</div>
    <div class="seg" id="detail-seg" style="margin:0 0 4px"></div>
    <div class="chart" id="detail-chart"></div>
    <div class="note">Recorded ${(series.coverage * 100).toFixed(0)}% of this window.
      ${dist.count} raw samples in the distribution below.</div>
    <div class="sub-h">Distribution</div>
    <div class="sub-p">Where the value actually sat over this window, from raw samples —
      the shape an average hides.</div>
    <div id="detail-hist"></div>
    <div class="explain"><h4>${spec.explain[0]}</h4><p>${spec.explain[1]}</p></div>`;

  document.getElementById("detail-seg").innerHTML = RANGES.map(([label, m]) =>
    `<button data-detail-m="${m}" class="${m === minutes ? "on" : ""}">${label}</button>`).join("");

  chart(document.getElementById("detail-chart"),
    [{ name: spec.label, color: css(spec.color), fill: css(spec.fill),
       points: series.points.map(v => v == null ? null : toChart(v)) }],
    { times: series.times, height: 200, format: spec.axisFmt,
      tip: spec.chartFmt, floor: spec.floor ?? null });
  histogram(document.getElementById("detail-hist"), dist.bins, css(spec.color),
    v => spec.tile(v) + " " + spec.unit, dist.overflowing);
}

/* ============================== path analysis ============================== */
async function showPath() {
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  modal.innerHTML = `
    <div class="modal-head"><h2>Where's the problem?</h2>
      <button class="close" data-dismiss>×</button></div>
    <p style="color:var(--text-2);font-size:12.5px;margin:10px 0 0">
      Tracing the path to the internet and reading the answer off the hop where the
      delay first appears. This takes up to half a minute.</p>
    <div class="empty" style="padding:18px 0">Tracing…</div>`;

  let result;
  try {
    result = await json("/api/path?target=1.1.1.1", { method: "POST" });
  } catch {
    modal.querySelector(".empty").textContent = "The trace failed.";
    return;
  }
  // A hop that never answered is refusing to reply, not dropping traffic — routers
  // deprioritise these packets, and calling that loss would invent a fault.
  const rows = result.hops.map(hop => `
    <div class="hop ${hop.n === result.culprit ? "blame" : ""} ${hop.silent ? "quiet" : ""}">
      <span class="n">${hop.n}</span><span class="h">${hop.host}</span>
      <span class="ms">${hop.silent ? "no reply" : hop.rtt_ms.toFixed(1) + " ms"}</span>
    </div>`).join("");

  modal.innerHTML = `
    <div class="modal-head"><h2>Where's the problem?</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="verdict-box ${result.where}" style="margin-top:16px">
      <h4>${result.summary}</h4><p>${result.detail}</p></div>
    <div class="sub-h" style="margin-top:0">The path</div>
    <div class="sub-p">Each hop, and how long a packet took to reach it and come back.</div>
    ${rows || `<div class="empty">No hops answered.</div>`}
    <div class="explain"><h4>Why a slow middle hop usually means nothing</h4>
      <p>Routers answer traceroute's probes at the lowest priority, so a hop can report
      400 ms while the traffic passing <em>through</em> it is fine — which is why only a
      rise that persists all the way to the end is counted here. A hop marked
      "no reply" is refusing to answer, not dropping your data.</p></div>`;
}

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
  const uptimeRange = event.target.closest("[data-days]");
  if (uptimeRange) {
    state.uptimeDays = Number(uptimeRange.dataset.days);
    for (const other of document.querySelectorAll("[data-days]"))
      other.classList.toggle("on", Number(other.dataset.days) === state.uptimeDays);
    drawUptime();
    return;
  }
  const tile = event.target.closest(".tile[data-metric]");
  if (tile) { openDetail(tile.dataset.metric); return; }
  if (event.target.closest("[data-dismiss]")) closeOverlays();
});
document.addEventListener("keydown", event => { if (event.key === "Escape") closeOverlays(); });

document.getElementById("tracepath").addEventListener("click", showPath);
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

refresh().then(() => { if (location.hash) openDetail(location.hash.slice(1)); });
setInterval(refresh, 15000);
try {
  const stream = new EventSource("/api/stream");
  let last = 0;
  stream.onmessage = () => { if (firstPaintDone && Date.now() - last > 4000) { last = Date.now(); refresh(); } };
} catch {}
