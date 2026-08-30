/* The dashboard: tiles, the three charts, and the right-hand instruments. */

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

/* A tile's number and its sparkline must be the same quantity. Ping success shows the
   inverse of what is stored, so without this the figure reads 100% while the line
   spikes upward on every packet lost — the two disagreeing on the same tile. */
function shownSeries(spec, points) {
  if (!spec.toChart) return points;
  return points.map(value => (value == null ? null : spec.toChart(value)));
}

function renderTiles(src) {
  const m = src.latest, sp = src.sparklines;
  let html = "";
  for (const [key, spec] of Object.entries(METRICS)) {
    if (m[key] == null) continue;
    html += `<button class="tile" data-metric="${key}"><span class="chev">›</span>
      <div class="label">${spec.label}</div>
      <div class="row"><span class="value">${spec.tile(m[key])}<span class="unit">${spec.unit}</span></span>
      <span class="spark">${sp[key] ? spark(shownSeries(spec, sp[key]), css(spec.color)) : ""}</span></div>
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
    html += `<button class="tile" data-speedtests="1"><span class="chev">›</span>
      <div class="label">Speed test</div>
      <div class="row"><span class="value">${fmt.mbps(m["speedtest.down_bytes_s"])}<span class="unit">Mbps ↓</span></span></div>
      <div class="caption">↑ ${fmt.mbps(m["speedtest.up_bytes_s"])} Mbps — past runs</div></button>`;
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
  // The panel always shows: it is the way into the devices-and-apps view, and hiding
  // it when the router lists nothing hides the button that explains why.
  document.getElementById("devcount").textContent = `${data.devices.length} · 24h`;
  document.getElementById("devices").innerHTML = data.devices.length === 0
    ? `<div class="empty">No devices reported — a router password lists them.</div>`
    : data.devices.map(d =>
    `<div class="row-item"><span style="font-weight:600">${d.name || "unnamed"}</span>
     <span class="when">${d.ip}</span><span style="flex:1"></span>
     <span class="when">${fmt.when(d.last_seen)}</span></div>`).join("");
}
