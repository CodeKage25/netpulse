/* The one place shared state lives.

   A router knows the radio and cannot see past its WAN port; a probe knows what the
   internet feels like and nothing about the radio. `owner` resolves each metric to
   whichever source actually reports it, so every view reads one connection rather
   than choosing between two partial ones. */

"use strict";
const RANGES = [["15m", 15], ["1h", 60], ["6h", 360], ["24h", 1440], ["7d", 10080]];
const state = {
  owner: {}, primary: null, minutes: 60, detail: null, detailMinutes: 60, uptimeDays: 7,
};
const UPTIME_RANGES = [["24h", 1], ["7d", 7], ["30d", 30]];

/* A router and a probe measure different halves of the same connection: the router
   knows signal, bands and data, and cannot see past its own WAN port; the probe knows
   what the internet actually feels like, and knows nothing about the radio. Making
   someone choose between them is what left the page looking empty, so each metric is
   resolved to whichever source actually reports it and the dashboard shows one
   connection rather than two partial ones. */
function resolveOwners(overview) {
  const owner = {};
  for (const source of overview.sources)
    for (const metric of Object.keys(source.latest))
      // First writer wins, and sources arrive router-first, so the router keeps the
      // radio metrics; latency and loss only ever come from the probe anyway.
      if (!(metric in owner)) owner[metric] = source.name;
  return owner;
}
