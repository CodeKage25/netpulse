/* Units, and the one place a design token is read.
   Every figure the dashboard prints passes through here, so "–" for absent is a single
   decision rather than twelve. */
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
