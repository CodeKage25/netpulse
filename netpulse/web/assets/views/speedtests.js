/* Past speed tests, and whether the link is getting worse. */

async function showSpeedtests() {
  const source = state.owner["speedtest.down_bytes_s"] || state.primary;
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  const data = await json(`/api/speedtests?source=${encodeURIComponent(source)}&days=90`);

  // A single number tells you what the link did once; whether it is getting worse is
  // the question worth asking, and it needs more than two points to answer.
  const trend = data.trend_pct == null ? ""
    : `<div class="verdict-box ${data.trend_pct < -15 ? "carrier" : data.trend_pct > 15 ? "clear" : "unknown"}">
         <h4>${data.trend_pct > 0 ? "Faster" : "Slower"} than it was, by ${Math.abs(data.trend_pct)}%</h4>
         <p>Comparing the newer half of these runs against the older half. Speed tests
         vary with the time of day and with whoever else is on the mast, so treat this
         as a direction rather than a measurement.</p></div>`;

  const rows = data.runs.map(run => `
    <div class="row-item"><span class="when" style="min-width:110px">${fmt.when(run.at)}</span>
      <span><b>${run.down_mbps}</b> Mbps ↓</span>
      <span class="dur">${run.up_mbps == null ? "" : run.up_mbps + " Mbps ↑"}</span></div>`).join("");

  modal.innerHTML = `
    <div class="modal-head"><h2>Speed tests</h2>
      <button class="close" data-dismiss>×</button></div>
    <p style="color:var(--text-2);font-size:12.5px;margin:10px 0 16px">
      ${data.count} run${data.count === 1 ? "" : "s"} in the last 90 days. Each one moved
      about 30 MB of real data, which is why they only happen when you ask.</p>
    ${trend}
    ${rows || `<div class="empty">No runs recorded yet.</div>`}
    <div class="explain"><h4>Why this disagrees with your carrier's app</h4>
      <p>This measures against Cloudflare, which is out on the public internet, while a
      carrier's own app usually tests against a server inside their network. Theirs
      measures the link; this measures the link plus everything between you and the
      places you actually use. Both are true, and the difference between them is
      itself worth knowing.</p></div>`;
}
