/* Who is on the network, and what is using it here.

   Two questions that sound like one and are not. The router knows which *devices* are
   connected; only this machine knows which *applications* are running on it. A router
   sees IP flows, not programs, so attributing bytes to an app from the router's side
   would mean inspecting traffic — which NetPulse does not do. The view keeps the two
   apart and labels each honestly rather than blending them into one number. */

async function showNetwork() {
  await ready;  // a source must be resolved; the dashboard need not have succeeded
  const source = state.primary;
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  modal.innerHTML = `
    <div class="modal-head"><h2>Network</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="empty" style="padding:24px 0">Reading the network…</div>`;

  const [network, apps] = await Promise.all([
    json(`/api/network?source=${encodeURIComponent(source)}&hours=24`).catch(() => null),
    json("/api/apps").catch(() => null),
  ]);
  if (!network) {
    modal.querySelector(".empty").textContent = "Could not read the network.";
    return;
  }
  renderNetwork(network, apps);
  // The first sample only established a baseline; come back once there is a delta.
  if (apps && apps.priming) {
    setTimeout(async () => {
      if (document.getElementById("modal-root").hidden) return;
      const fresh = await json("/api/apps").catch(() => null);
      if (fresh) renderNetwork(network, fresh);
    }, 5000);
  }
}

function renderNetwork(network, apps) {
  const devices = network.devices || [];
  const rows = devices.map(device => {
    const label = device.name || "unnamed device";
    // A locally-administered MAC is a privacy address, not a manufacturer's. Saying
    // "private address" beats guessing a vendor that would be wrong.
    const octet = parseInt((device.mac || "00").slice(0, 2), 16);
    const privateMac = Number.isFinite(octet) && (octet & 2);
    const usage = device.down_bytes == null
      ? `<span class="when">not reported</span>`
      : `<span class="when" style="color:var(--text)">↓ ${fmt.bytes(device.down_bytes)}
         · ↑ ${fmt.bytes(device.up_bytes)}${device.measured_here ? " · measured here" : ""}</span>`;
    const action = !network.can_block ? ""
      : `<button class="btn ${device.blocked ? "" : "danger"}"
           data-block="${device.mac}" data-on="${device.blocked ? 0 : 1}"
           data-label="${label.replace(/"/g, "")}"
           style="padding:4px 10px;font-size:11.5px">${device.blocked ? "Unblock" : "Block"}</button>`;
    return `<div class="row-item" style="align-items:center">
      <span class="pulse-dot ${device.blocked ? "down" : ""}" style="width:7px;height:7px"></span>
      <div style="min-width:0">
        <div style="font-weight:600">${label}${device.self ? " · this machine" : ""}${
          device.blocked ? " · blocked" : ""}</div>
        <div class="when">${device.ip || "no lease"} · ${device.mac}${
          privateMac ? " · private address" : ""}${
          device.last_seen ? " · seen " + fmt.when(device.last_seen) : ""}</div>
      </div>
      <span style="flex:1"></span>
      ${usage}
      ${action}
    </div>`;
  }).join("");

  const appRows = !apps || !apps.available
    ? `<div class="empty">Per-application usage needs macOS or Linux.</div>`
    : apps.priming
      ? `<div class="empty">Measuring — the first sample is a baseline, so usage
           appears a few seconds from now.</div>`
    : (apps.apps.length
        ? apps.apps.map(app => `<div class="row-item" style="align-items:center">
            <span style="font-weight:600">${app.name}</span>
            ${app.system ? '<span class="tag">system</span>' : ""}
            <span style="flex:1"></span>
            <span class="when">↓ ${fmt.bytes(app.down_bytes)} · ↑ ${fmt.bytes(app.up_bytes)}</span>
          </div>`).join("")
        : `<div class="empty">Nothing has moved since the last sample.</div>`);

  document.getElementById("modal").innerHTML = `
    <div class="modal-head"><h2>Network</h2>
      <button class="close" data-dismiss>×</button></div>

    <div class="sub-h" style="margin-top:16px">Devices</div>
    <div class="sub-p">Seen by the router in the last 24 hours.</div>
    ${rows || `<div class="empty">No devices reported. A router password is needed to list them.</div>`}

    ${!network.others ? "" : `<div class="row-item" style="align-items:center">
      <span style="font-weight:600">Everything except this machine</span>
      <span class="when">today · the connection's own counter minus what this
        machine used</span>
      <span style="flex:1"></span>
      <span class="when">${fmt.bytes(network.others.bytes)}</span></div>
    <div class="sub-p" style="margin-top:6px">Which of the devices above that was,
      nobody here can say — this is the total they moved between them. The router counts
      frames on the wire and this machine counts bytes through sockets, so the figure
      carries that difference and reads a little high.</div>`}

    ${network.per_device_bytes === "router" ? "" : `<div class="explain" style="margin-top:14px">
      <h4>Why only one device shows usage</h4>
      <p>Per-device traffic has to come from whatever sits in the path. This router
      publishes a byte counter for every client and leaves every one at zero — checked
      repeatedly under live traffic, and its own web interface never displays the field
      either. So it is not being withheld; it is not measured.</p>
      <p style="margin-top:8px">NetPulse can only measure the machine it runs on, and
      does — that row is real, and the applications below are its breakdown. For the
      rest, the honest options are a router that reports per-client counters (OpenWrt
      with nlbwmon, MikroTik, many Huawei models), or running NetPulse on those devices
      too. Splitting the connection total between devices by presence would produce a
      number for every row and a fact for none.</p></div>`}

    <div class="sub-h">Where the data went</div>
    <div class="sub-p">Last 24 hours on ${apps ? apps.host : "this machine"}, busiest
      first — by service rather than by program.</div>
    ${serviceRows(network.services || [])}

    <div class="sub-h">Applications on ${apps ? apps.host : "this machine"}</div>
    <div class="sub-p">Since the last sample, busiest first. This machine only —
      the router cannot see which programs are running on anything.</div>
    ${appRows}

    <div class="explain"><h4>Devices and applications are different questions</h4>
      <p>The router knows what is <em>connected</em>; only a machine knows what is
      <em>running</em> on it. Blending the two would produce a single list that is wrong
      in both directions, so they stay apart — and the totals here will not sum to the
      connection's throughput, because the other devices are not reporting theirs.</p></div>`;
}

/* "Which service" and "which program" are different questions. A browser is one
   application whether it is streaming a film or idle, so a list of process names can
   put Chrome at the top every day and never once explain the bill.

   A name here is only as good as what the address said. Some destinations identify a
   service outright; others are shared content networks carrying millions of unrelated
   sites, and for those the honest caption is how the traffic travelled, not what it
   was for. Rendering both the same way would imply a certainty that only half of them
   have, so the uncertain ones say so on their own row. */
function serviceRows(services) {
  if (!services.length) {
    return `<div class="empty">Nothing recorded yet — this fills in as traffic
      is measured.</div>`;
  }
  const CAVEAT = {
    network: "shared content network — the address does not say which site",
    cloud: "rented hosting — the address does not say whose service",
    "": "not in the address table",
  };
  return services.map(service => {
    const caveat = service.identified ? "" : CAVEAT[service.kind] ?? CAVEAT[""];
    return `<div class="row-item" style="align-items:center">
      <span style="font-weight:600${service.identified ? "" : ";color:var(--text-2)"}">
        ${service.key}</span>
      ${caveat ? `<span class="when">${caveat}</span>` : ""}
      <span style="flex:1"></span>
      <span class="when">↓ ${fmt.bytes(service.down)} · ↑ ${fmt.bytes(service.up)}</span>
    </div>`;
  }).join("");
}

/* Blocking is a write to somebody's router, so it is confirmed, then re-read rather
   than assumed: the button reflects what the router says afterwards, not what was
   asked for. */
async function toggleBlock(button) {
  const mac = button.dataset.block;
  const on = button.dataset.on === "1";
  const label = button.dataset.label || "";
  const question = on
    ? `Block ${label}?\n\nIt will lose the connection until you unblock it.`
    : `Unblock ${label}?`;
  if (!confirm(question)) return;

  button.disabled = true;
  button.textContent = on ? "Blocking…" : "Unblocking…";
  const query = `source=${encodeURIComponent(state.primary)}&mac=${encodeURIComponent(mac)}` +
                `&on=${on ? 1 : 0}&label=${encodeURIComponent(label)}`;
  try {
    const result = await json(`/api/block?${query}`, { method: "POST" });
    if (result.error) { alert(result.error); }
  } catch {
    alert("The router did not accept that.");
  }
  showNetwork();
}
