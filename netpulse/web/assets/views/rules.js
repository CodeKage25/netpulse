/* Data rules: what each one says right now.

   A verdict is a statement about this moment, so the view asks for it rather than
   remembering it — a rule shown as holding a device after its allowance rolled over
   would be worse than showing nothing. */

async function showRules() {
  await ready;
  const source = state.primary;
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  modal.innerHTML = `
    <div class="modal-head"><h2>Rules</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="empty" style="padding:24px 0">Checking…</div>`;

  const data = await json(`/api/rules?source=${encodeURIComponent(source)}`).catch(() => null);
  if (!data) {
    modal.querySelector(".empty").textContent = "Could not read the rules.";
    return;
  }
  renderRules(data);
}

const RULE_KINDS = {
  limit: "allowance",
  schedule: "timetable",
  timer: "countdown",
};

function renderRules(data) {
  const rows = data.rules.map(rule => {
    // An allowance nobody has measured is not an allowance at zero. Drawing an empty
    // bar under a device the router has never reported says "it has used nothing",
    // which is a measurement — and one nothing here has made.
    const unmeasured = rule.limit_bytes && rule.used_bytes == null;
    const share = rule.limit_bytes && rule.used_bytes != null
      ? Math.min(1, rule.used_bytes / rule.limit_bytes)
      : null;
    const tone = !rule.holds ? "" : rule.blocks ? "over" : "warn";
    const meter = unmeasured ? `
      <div style="font-size:11px;color:var(--text-3);margin:5px 0 2px">
        Allowance ${fmt.bytes(rule.limit_bytes)} — nothing measured for
        ${rule.devices.length > 1 ? "these devices" : "this device"} this cycle.</div>`
      : share == null ? "" : `
      <div class="meter" style="margin:7px 0 4px">
        <i class="${tone}" style="width:${(share * 100).toFixed(1)}%"></i>
      </div>
      <div style="font-size:11px;color:var(--text-3)">
        ${fmt.bytes(rule.used_bytes)} of ${fmt.bytes(rule.limit_bytes)}
        ${rule.pooled ? " · shared across the group" : ""}</div>`;

    // A rule that only reports is doing its job when it holds; one that blocks has
    // acted. Saying "blocked" for the first would be a claim about the network that
    // is not true.
    const status = !rule.holds
      ? `<span class="when">within limits</span>`
      : rule.blocks
        ? `<span style="color:var(--critical);font-size:12px;font-weight:650">blocking</span>`
        : `<span style="color:var(--warning);font-size:12px;font-weight:650">over — not blocking</span>`;

    return `<div class="row-item" style="align-items:flex-start;flex-direction:column;gap:2px">
      <div style="display:flex;align-items:center;gap:9px;width:100%">
        <span style="font-weight:600">${rule.name}</span>
        <span class="tag">${RULE_KINDS[rule.kind] || rule.kind}</span>
        ${rule.enabled ? "" : '<span class="tag">off</span>'}
        <span style="flex:1"></span>
        ${status}
      </div>
      <div class="when">${rule.devices.join(" · ")} — ${rule.reason}</div>
      ${meter}
    </div>`;
  }).join("");

  document.getElementById("modal").innerHTML = `
    <div class="modal-head"><h2>Rules</h2>
      <button class="close" data-dismiss>×</button></div>

    ${data.count === 0 ? "" : `<p style="color:var(--text-2);font-size:12.5px;margin:10px 0 4px">
      ${data.count} rule${data.count === 1 ? "" : "s"}, checked just now. A rule does not
      fire and stay fired — at any moment it either holds a device or it does not.</p>`}

    ${rows || `<div class="empty">No rules configured.</div>`}

    <div class="explain"><h4>Writing a rule</h4>
      <p>Rules live in <code>~/.netpulse/netpulse.toml</code>. An <b>allowance</b> is a
      quantity, a <b>timetable</b> is a set of hours, a <b>countdown</b> runs from when
      you save it. A group either pools one budget and runs out together, or gives each
      member the whole thing.</p>
      <pre style="margin:10px 0 0;padding:11px 13px;background:var(--panel-2);
        border:1px solid var(--border);border-radius:9px;overflow-x:auto;
        font-size:11.5px;line-height:1.55">[[rule]]
name = "kids tablets"
kind = "limit"
devices = ["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]
limit_gb = 20
pooled = true
block = true</pre>
      <p style="margin-top:9px"><code>block</code> is opt-in. Without it a rule watches
      and reports, which is useful on its own — turning every allowance into an
      enforcement action by default would be a surprising thing to do to a network.</p>
    </div>`;
}
