/* The per-metric drill-down: hero figures, its own range, the series, the
   distribution the average hides, and what the number actually means. */

async function openDetail(metric) {
  if (!METRICS[metric]) return;
  // The same wait every other linked view does. Without it a deep link opens before
  // any source is resolved, and the panel queries "" — which returns nothing and reads
  // as "no data" rather than "asked the wrong question".
  await ready;
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
  //
  // And "best" is not "smallest". For latency and loss, less is better; for signal and
  // throughput, more is. Without asking the metric which way it runs, a signal panel
  // reports -96 dBm as its best hour and -90 as its worst, which is exactly backwards.
  // The flag describes the stored metric, not the displayed one — loss is stored and
  // success is shown, and reading the flag off the display inverts it again.
  // Everything on this panel goes through the same transform, so a metric stored one
  // way up and shown the other cannot disagree with itself.
  const shown = value => (value == null ? null : toChart(value));
  const bestRaw = spec.higherIsBetter ? dist.max : dist.min;
  const worstRaw = spec.higherIsBetter ? dist.min : dist.max;
  const best = shown(bestRaw);
  const worst = shown(worstRaw);
  // Some metrics measure how well the connection is working; others measure how much
  // of it is being used. Calling an idle minute the "worst" download contradicts the
  // explainer directly beneath it, which says an idle link reads near zero however
  // fast it is. Those metrics name their extremes themselves.
  const [highLabel, lowLabel] = spec.extremes || ["Best", "Worst"];
  const heroes = [
    ["Current", live == null ? "–" : spec.tile(live)],
    ["Average", dist.mean == null ? "–" : spec.tile(dist.mean)],
    [highLabel, bestRaw == null ? "–" : spec.tile(bestRaw)],
    [lowLabel, worstRaw == null ? "–" : spec.tile(worstRaw)],
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
      tip: spec.chartFmt, floor: spec.floor ?? null, ceiling: spec.ceiling ?? null });
  // Bins are raw, so they are relabelled through the metric's own formatter and, where
  // the axis runs the other way, reversed — otherwise a success panel reads 100% on the
  // left and 75% on the right while the bars climb the wrong way.
  const bins = spec.invertAxis
    ? [...dist.bins].reverse().map(bin => ({ ...bin, lo: bin.hi, hi: bin.lo }))
    : dist.bins;
  histogram(document.getElementById("detail-hist"), bins, css(spec.color),
    v => spec.tile(v) + " " + spec.unit, dist.overflowing);
}
