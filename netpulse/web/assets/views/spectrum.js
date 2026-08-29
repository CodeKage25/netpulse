/* The radio, in three dimensions — because this data genuinely has them.

   Dishylink's dome is 3D for a good reason: sky obstruction is spherical, so a sphere
   is the honest shape for it. Copying that for a cellular router would be decoration,
   since nothing about a modem is spatial in that way.

   But carrier aggregation is. Each component carrier occupies a real position in the
   spectrum at a real width, and the stack changes through the day as the network hands
   carriers out and takes them back. That is a surface over frequency and time, and it
   is the answer to the most common unexplained complaint there is: the signal did not
   move, so why did the speed halve? Because you were on 180 MHz and now you are on 80.

   Honest about what is measured. Frequency and width are read from the router, per
   carrier, exactly. Height is the *leg's* signal — the router reports one RSRP for its
   LTE carriers together and one for 5G — so the bars within a leg share a height and
   the panel says so rather than implying five separate measurements. */

/* Drawn width floor. Below this a carrier is a hairline at any sane zoom, and a
   chart nobody can see is not more honest than one with a stated minimum. */
const MIN_VISIBLE_MHZ = 60;
const SPECTRUM_FLOOR_DBM = -120;
const SPECTRUM_CEILING_DBM = -60;

function signalHeight(dbm) {
  if (dbm == null) return 0.08;
  const span = SPECTRUM_CEILING_DBM - SPECTRUM_FLOOR_DBM;
  return 0.08 + 0.52 * Math.max(0, Math.min(1, (dbm - SPECTRUM_FLOOR_DBM) / span));
}

/* The whole scene sits below the horizon otherwise: geometry grows upward from the
   floor, so without lifting the camera's focus the mass renders in the top half with
   empty space beneath it. */
const FLOOR_Y = -0.34;

/* Colour by SINR, which is what actually predicts throughput: a strong carrier full of
   interference carries less than a weak clean one. */
function sinrColor(db) {
  if (db == null) return css("--text-3");
  if (db >= 13) return css("--good");
  if (db >= 5) return css("--up");
  if (db >= 0) return css("--warning");
  return css("--critical");
}

let spectrumScene = null;

async function showSpectrum() {
  // Opened by a link, this can run before the first refresh has resolved a source, so
  // it asks rather than assuming the dashboard got there first.
  await ready;  // a source must be resolved; the dashboard need not have succeeded
  const source = state.owner["radio.aggregate_mhz"]
    || state.owner["radio.cc0.mhz"]
    || state.primary;
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  modal.innerHTML = `
    <div class="modal-head"><h2>Spectrum</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="empty" style="padding:24px 0">Reading the carrier stack…</div>`;

  let data;
  try {
    // Fewer, thicker slices: at 48 each carrier renders as a hairline ribbon and the
    // eye reads streaks rather than blocks. Twenty is enough to see the stack change.
    data = await json(
      `/api/spectrum?source=${encodeURIComponent(source)}&minutes=${state.minutes}&slices=20`);
  } catch {
    modal.querySelector(".empty").textContent = "Could not read the spectrum.";
    return;
  }
  if (!data.supported) {
    modal.innerHTML = `
      <div class="modal-head"><h2>Spectrum</h2>
        <button class="close" data-dismiss>×</button></div>
      <div class="explain" style="margin-top:16px"><h4>This connection does not report its carriers</h4>
        <p>Cellular routers publish which frequencies they are aggregating; a Starlink
        dish, a fibre ONT or a plain internet probe have nothing equivalent to show.
        Nothing is wrong — there is simply no spectrum here to draw.</p></div>`;
    return;
  }

  const filled = data.slices.filter(slice => slice.carriers.length);
  const signal = data.signal || {};
  modal.innerHTML = `
    <div class="modal-head"><h2>Spectrum</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="heroes">
      <div class="hero"><div class="n">${data.aggregate_mhz ?? "–"}<span>MHz</span></div>
        <div class="l">aggregated now</div></div>
      <div class="hero"><div class="n">${data.carriers}</div>
        <div class="l">carriers</div></div>
      <div class="hero"><div class="n">${signal.lte_rsrp ?? "–"}<span>dBm</span></div>
        <div class="l">LTE anchor</div></div>
      ${signal.nr_rsrp == null ? "" : `<div class="hero"><div class="n">${signal.nr_rsrp}<span>dBm</span></div>
        <div class="l">5G carrier</div></div>`}
    </div>
    <div class="scene-wrap">
      <canvas id="spectrum-canvas"></canvas>
      <div class="scene-readout" id="spectrum-readout">Drag to orbit · scroll to zoom</div>
    </div>
    <div class="legend" style="margin-top:10px;flex-wrap:wrap">
      <span><span class="dot" style="background:var(--good)"></span>SINR 13+ dB</span>
      <span><span class="dot" style="background:var(--up)"></span>5–13</span>
      <span><span class="dot" style="background:var(--warning)"></span>0–5</span>
      <span><span class="dot" style="background:var(--critical)"></span>below 0</span>
    </div>
    <div class="note" id="spectrum-note"></div>
    <div class="note" style="margin-top:2px">Blocks narrower than ${MIN_VISIBLE_MHZ} MHz
      are drawn at that width so they stay visible; their widths relative to each other
      are true.</div>
    <div class="explain"><h4>What this shows that a signal bar cannot</h4>
      <p>Your router is not on one frequency. It aggregates several carriers at once,
      and the network adds and removes them constantly — during congestion, when you
      move, when a cell reconfigures. Losing a 20 MHz carrier takes a fifth of your
      capacity away while the signal strength does not move at all, which is why speed
      changes so often look inexplicable. Height is the leg's signal rather than each
      carrier's: the router reports one figure for its LTE carriers together and one
      for 5G, so carriers within a leg share a height honestly rather than pretending
      to five separate measurements.</p></div>`;

  // How often the stack actually changed shape — the thing worth knowing, and the
  // reason to look at this at all.
  const shapes = filled.map(slice =>
    slice.carriers.map(c => Math.round(c.mhz)).sort().join(","));
  const changes = shapes.filter((shape, index) => index && shape !== shapes[index - 1]).length;
  const widths = filled.map(slice =>
    slice.carriers.reduce((sum, c) => sum + (c.bw_mhz || 0), 0));
  const narrowest = Math.min(...widths);
  const widest = Math.max(...widths);
  document.getElementById("spectrum-note").innerHTML =
    `Each block is one carrier at its true centre frequency, drawn at its real
     bandwidth; depth is time. ${filled.length} of ${data.slices.length} slices
     recorded. ` + (changes
      ? `The stack changed <b>${changes}</b> time${changes === 1 ? "" : "s"} here,
         between ${narrowest} and ${widest} MHz — a ${Math.round(
           (1 - narrowest / Math.max(widest, 1)) * 100)}% swing in capacity with the
         signal unchanged.`
      : `The stack held steady at ${widest} MHz throughout.`);

  const canvas = document.getElementById("spectrum-canvas");
  spectrumScene = createScene(canvas);
  spectrumScene.onHover(box => {
    const readout = document.getElementById("spectrum-readout");
    if (!readout) return;
    readout.textContent = box
      ? `${box.label} · ${box.mhz.toFixed(2)} MHz · ${box.bw} MHz wide · PCI ${box.pci ?? "–"} · ${box.when}`
      : "Drag to orbit · scroll to zoom";
  });
  renderSpectrum(data, filled);
  // Turn once on open: static 3D reads as a flat pattern until it moves.
  requestAnimationFrame(() => spectrumScene && spectrumScene.introduce());
}

function renderSpectrum(data, filled) {
  if (!filled.length) {
    spectrumScene.set([], []);
    return;
  }
  // The frequency axis spans only what is actually in use, padded a little, so a link
  // sitting entirely on low bands is not drawn as a sliver at the far left.
  const all = filled.flatMap(slice => slice.carriers);
  const lowest = Math.min(...all.map(c => c.mhz - (c.bw_mhz || 20) / 2));
  const highest = Math.max(...all.map(c => c.mhz + (c.bw_mhz || 20) / 2));
  const span = Math.max(1, highest - lowest);
  const toX = mhz => ((mhz - lowest) / span - 0.5) * 2.4;

  const signal = data.signal || {};
  const boxes = [];
  filled.forEach((slice, index) => {
    // Newest at the front, oldest receding — the direction people read time on a chart.
    const age = (filled.length - 1 - index) / Math.max(1, filled.length - 1);
    const z0 = -age * 1.55 - 0.02;
    // Nearly touching, so consecutive slices read as one surface moving through time
    // rather than as separate objects.
    const z1 = z0 + (1.55 / Math.max(filled.length, 6)) * 0.92;
    const when = fmt.clock(slice.at);
    for (const carrier of slice.carriers) {
      const width = carrier.bw_mhz || 20;
      // A 20 MHz carrier is 0.7% of the span drawn, which is honest and invisible.
      // Every block gets the same minimum, so their *relative* widths stay true — a
      // 100 MHz carrier still draws five times a 20 MHz one wherever both clear it.
      const half = Math.max(width, MIN_VISIBLE_MHZ) / 2;
      const rsrp = carrier.nr ? signal.nr_rsrp : signal.lte_rsrp;
      const sinr = carrier.nr ? signal.nr_sinr : signal.lte_sinr;
      boxes.push({
        x0: toX(carrier.mhz - half),
        x1: toX(carrier.mhz + half),
        y0: FLOOR_Y,
        y1: FLOOR_Y + signalHeight(rsrp),
        z0,
        z1,
        color: sinrColor(sinr),
        // The oldest slices recede in tone as well as depth, so "now" reads as now.
        dim: 0.45 + 0.55 * (1 - age),
        label: (carrier.nr ? "n" : "B") + (carrier.band ?? "?"),
        mhz: carrier.mhz,
        bw: width,
        pci: carrier.pci,
        when,
      });
    }
  });

  // Frequency ticks along the front edge, in real megahertz. Carriers on adjacent
  // channels of the same band would otherwise print their labels on top of each other,
  // so anything within a hair of its neighbour is nudged along the axis.
  const newest = filled[filled.length - 1];
  const labels = [];
  const sorted = [...newest.carriers].sort((a, b) => a.mhz - b.mhz);
  let previousX = -Infinity;
  let row = 0;
  sorted.forEach(carrier => {
    const x = toX(carrier.mhz);
    // Two carriers on adjacent channels of one band sit almost on top of each other,
    // so a label that would collide drops to a second row in front rather than being
    // nudged sideways to a frequency it does not belong to.
    row = x - previousX < 0.5 ? (row + 1) % 2 : 0;
    previousX = x;
    labels.push({
      x,
      y: FLOOR_Y - 0.1 - row * 0.11,
      z: 0.34 + row * 0.34,
      text: `${(carrier.nr ? "n" : "B") + (carrier.band ?? "?")} · ${Math.round(carrier.mhz)}`,
      color: css("--text-2"),
      size: 11,
    });
  });

  // Height ticks at round dBm values, so a bar's height is a quantity you can read
  // off rather than a shape you compare to its neighbour.
  const dbmTicks = [-110, -95, -80, -65];
  for (const dbm of dbmTicks) {
    labels.push({
      x: -1.42,
      y: FLOOR_Y + signalHeight(dbm),
      z: 0.28,
      text: `${dbm}`,
      color: css("--text-3"),
      size: 10,
      align: "right",
    });
  }
  labels.push({
    // Above the topmost tick, not on it — the two overlapped at this camera angle.
    x: -1.46, y: FLOOR_Y + signalHeight(-48), z: 0.28,
    text: "dBm", color: css("--text-3"), size: 10, align: "right",
  });

  // The time axis: only the two ends are labelled. Intermediate stamps on a receding
  // axis crowd into illegibility long before they become useful.
  labels.push({
    x: 1.62, y: FLOOR_Y + 0.06, z: 0.28,
    text: "now", color: css("--text-2"), size: 10.5, align: "left",
  });
  labels.push({
    x: 1.62, y: FLOOR_Y + 0.06, z: -1.55,
    text: fmt.hm(filled[0].at), color: css("--text-3"), size: 10.5, align: "left",
  });

  spectrumScene.set(boxes, labels, {
    y: FLOOR_Y,
    x0: -1.35, x1: 1.35, z0: -1.6, z1: 0.3,
    // A line under each carrier rather than an even grid: the gridlines that help are
    // the ones that say where the carriers are.
    xs: [...new Set(newest.carriers.map(c => toX(c.mhz)))],
    zs: [-1.55, -1.1, -0.65, -0.2],
    ticks: dbmTicks.map(dbm => ({ y: FLOOR_Y + signalHeight(dbm) })),
    grid: css("--grid"),
    edge: css("--border-2"),
    now: css("--text-3"),
  }, css("--panel"));
}
