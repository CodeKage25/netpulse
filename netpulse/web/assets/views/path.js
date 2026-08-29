/* Where the delay starts, and where certainty runs out. */

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
