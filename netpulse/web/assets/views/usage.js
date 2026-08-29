/* Data usage, broken down three ways — and kept apart, because they are measured by
   three different things and will not add up to each other.

   The daily total is the router's own odometer, so it is complete for any day NetPulse
   was watching. The application rows are this machine only. The device rows exist only
   where a router publishes per-client counters. Summing across them would produce a
   number that looks authoritative and is not. */

async function showUsage() {
  const source = state.owner["data.month_total_bytes"]
    || state.owner["data.month_down_bytes"]
    || state.primary;
  const modal = document.getElementById("modal");
  document.getElementById("modal-root").hidden = false;
  modal.innerHTML = `
    <div class="modal-head"><h2>Data usage</h2>
      <button class="close" data-dismiss>×</button></div>
    <div class="empty" style="padding:24px 0">Reading the history…</div>`;

  const data = await json(
    `/api/usage?source=${encodeURIComponent(source)}&days=${state.usageDays}`).catch(() => null);
  if (!data) {
    modal.querySelector(".empty").textContent = "Could not read the usage history.";
    return;
  }
  renderUsage(data);
}

function renderUsage(data) {
  const days = data.days || [];
  const measured = days.filter(day => day.bytes != null);
  const peak = Math.max(1, ...measured.map(day => day.bytes));
  const total = measured.reduce((sum, day) => sum + day.bytes, 0);

  const bars = days.map(day => {
    const known = day.bytes != null;
    const height = known ? Math.max(2, (day.bytes / peak) * 100) : 0;
    // A day nobody recorded gets a hatched placeholder at full height, not a short bar
    // — a short bar reads as "a quiet day", which is the opposite of what it means.
    const label = new Date(day.day + "T00:00:00").toLocaleDateString([], {
      weekday: "short", day: "numeric",
    });
    const partial = known && day.coverage < 0.9;
    return `<div class="day" title="${day.day}">
      <div class="day-bar">
        ${known
          ? `<i style="height:${height}%${partial ? ";opacity:.55" : ""}"></i>`
          : `<u></u>`}
      </div>
      <div class="day-value">${known ? fmt.bytes(day.bytes) : "–"}</div>
      <div class="day-label">${label}${partial ? "*" : ""}</div>
    </div>`;
  }).join("");

  const anyPartial = measured.some(day => day.coverage < 0.9);
  const rows = (entries, empty) => entries.length
    ? entries.map(entry => `<div class="row-item" style="align-items:center">
        <span style="font-weight:600">${entry.key}</span>
        <span style="flex:1"></span>
        <span class="when">↓ ${fmt.bytes(entry.down)} · ↑ ${fmt.bytes(entry.up)}</span>
      </div>`).join("")
    : `<div class="empty">${empty}</div>`;

  document.getElementById("modal").innerHTML = `
    <div class="modal-head"><h2>Data usage</h2>
      <button class="close" data-dismiss>×</button></div>

    <div class="heroes">
      <div class="hero"><div class="n">${fmt.bytes(total)}</div>
        <div class="l">over ${measured.length} recorded day${measured.length === 1 ? "" : "s"}</div></div>
      <div class="hero"><div class="n">${measured.length
        ? fmt.bytes(total / measured.length) : "–"}</div>
        <div class="l">a day, on average</div></div>
    </div>

    <div class="seg" id="usage-seg" style="margin:0 0 4px"></div>
    <div class="days">${bars}</div>
    <div class="note">Counted by the router, so a recorded day is complete.
      ${anyPartial ? "Days marked * were only partly recorded — they are shown faded, "
        + "and their totals cover the hours NetPulse was running rather than the whole day."
        : ""}
      Days are UTC, which may not line up with your midnight.</div>

    <div class="sub-h">Applications · ${data.host}</div>
    <div class="sub-p">This machine only. The router cannot see which programs run on
      anything, so these will not add up to the daily totals above.</div>
    ${rows(data.apps, "Nothing recorded yet — usage appears as NetPulse runs.")}

    <div class="sub-h">Devices</div>
    <div class="sub-p">Only where the router publishes a per-client counter.</div>
    ${rows(data.devices, "This router does not report per-device traffic.")}

    <div class="explain"><h4>Why these three do not add up</h4>
      <p>The daily figures come from the router's own odometer and cover everything on
      the connection. The application rows are measured on this machine alone. The
      device rows exist only if the router counts per client, and most do not. Three
      honest measurements of three different things beat one total that quietly
      apportions what nobody counted.</p></div>`;

  document.getElementById("usage-seg").innerHTML = USAGE_RANGES.map(([label, count]) =>
    `<button data-usage-days="${count}" class="${count === state.usageDays ? "on" : ""}">${label}</button>`
  ).join("");
}
