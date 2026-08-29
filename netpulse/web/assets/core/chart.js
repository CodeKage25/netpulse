/* Chart primitives: honest gaps, nice axes, a crosshair.
   Knows nothing about which metric it is drawing — callers hand it points and colours. */
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
