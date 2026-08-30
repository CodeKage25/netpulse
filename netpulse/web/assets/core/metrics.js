/* One spec per metric drives its tile, its chart and its detail view, so adding a
   metric is a row here rather than three places that must be kept in agreement. */
const METRICS = {
  "traffic.down_bytes_s": {
    higherIsBetter: true,
    extremes: ["Peak", "Quietest"],
    label: "Download", unit: "Mbps", color: "--down", fill: "--down-fill",
    tile: m => fmt.mbps(m), caption: "current traffic",
    toChart: v => v * 8 / 1e6, chartFmt: v => v.toFixed(1) + " Mbps", axisFmt: fmt.axis,
    explain: ["What is throughput?",
      "How much data is crossing the link right now, read from the router's own counters " +
      "every few seconds. It measures what your devices are actually using, not how fast " +
      "the connection could go — an idle link reads near zero however fast it is. For " +
      "capacity, run a speed test."],
  },
  "traffic.up_bytes_s": {
    higherIsBetter: true,
    extremes: ["Peak", "Quietest"],
    label: "Upload", unit: "Mbps", color: "--up", fill: "--up-fill",
    tile: m => fmt.mbps(m), caption: "current traffic",
    toChart: v => v * 8 / 1e6, chartFmt: v => v.toFixed(1) + " Mbps", axisFmt: fmt.axis,
    explain: ["What is upload?",
      "Data leaving your network — video calls, backups, sending photos. It is usually a " +
      "fraction of download capacity on mobile networks, and it is what suffers first when " +
      "a cell is congested."],
  },
  "latency.internet_ms": {
    label: "Latency", unit: "ms", color: "--mono", fill: "--mono-fill",
    tile: m => Math.round(m), caption: "TCP connect, live",
    chartFmt: fmt.ms, axisFmt: v => Math.round(v), floor: 0,
    explain: ["What is latency?",
      "How long it takes to open a connection to a well-known internet address and get " +
      "an answer back. That is a TCP handshake rather than a ping, deliberately: ping " +
      "asks whether a packet can make the trip, while this asks whether a connection " +
      "can actually be established — which is what a browser, a call or a game has to " +
      "do first. It therefore reads a little higher than ping, and notices problems " +
      "ping cannot. Spikes matter far more than the average, so the chart keeps the " +
      "worst value in each bucket rather than smoothing it away."],
  },
  "signal.rsrp_dbm": {
    higherIsBetter: true,
    label: "Signal", unit: "dBm", color: "--mono", fill: "--mono-fill",
    tile: m => Math.round(m), caption: "RSRP at the router",
    chartFmt: v => v.toFixed(1) + " dBm", axisFmt: v => Math.round(v), floor: -120,
    explain: ["What is RSRP?",
      "The raw strength of the tower's signal reaching your router, in dBm — a negative " +
      "number where closer to zero is stronger. Better than −90 is strong, −90 to −105 is " +
      "usable, below −110 will drop. Moving the router near a window, or higher, usually " +
      "moves this number more than anything else you can do."],
  },
  "signal.sinr_db": {
    higherIsBetter: true,
    label: "Quality", unit: "dB", color: "--accent", fill: "--accent-fill",
    tile: m => m.toFixed(0), caption: "SINR — higher is cleaner",
    chartFmt: v => v.toFixed(1) + " dB", axisFmt: v => Math.round(v), floor: -5,
    explain: ["What is SINR?",
      "How clean the signal is against noise and neighbouring cells. Strength without " +
      "quality still gives you a slow connection, which is why a full-bars router can " +
      "crawl at busy hours. Above 20 dB is excellent, 13–20 good, below 5 dB is where " +
      "throughput collapses even on a strong signal."],
  },
  "signal.rsrp_5g_dbm": {
    higherIsBetter: true,
    label: "5G signal", unit: "dBm", color: "--down", fill: "--down-fill",
    tile: m => Math.round(m), caption: "RSRP on the 5G carrier",
    chartFmt: v => v.toFixed(1) + " dBm", axisFmt: v => Math.round(v), floor: -120,
    explain: ["What is the 5G carrier?",
      "On 5G non-standalone your router holds an LTE anchor and a 5G carrier at the " +
      "same time, and the 5G leg does most of the carrying. Its strength can differ " +
      "sharply from the anchor's, which is why both are tracked separately — a strong " +
      "anchor with a weak 5G leg still feels slow."],
  },
  "signal.sinr_5g_db": {
    higherIsBetter: true,
    label: "5G quality", unit: "dB", color: "--accent", fill: "--accent-fill",
    tile: m => m.toFixed(0), caption: "SINR on the 5G carrier",
    chartFmt: v => v.toFixed(1) + " dB", axisFmt: v => Math.round(v), floor: -5,
    explain: ["Why does 5G quality matter more than strength?",
      "The 5G carrier uses wide channels at high frequency, where interference costs " +
      "more than distance. A low SINR here collapses throughput even while the signal " +
      "bars stay full, and it is the usual explanation for a fast connection that " +
      "crawls at busy hours."],
  },
  "loss.pct": {
    label: "Ping success",
    unit: "%",
    color: "--good",
    fill: "--mono-fill",
    // NOTE the direction: `higherIsBetter` describes the *stored* metric, which is
    // loss — and less loss is better. The panel shows its inverse, and `tile` handles
    // that. Setting this from the displayed value instead reported 50% as the best
    // hour and 100% as the worst.
    higherIsBetter: false,
    invertAxis: true,
    // The stored metric is loss; the panel is about success. `toChart` inverts it so
    // the series, its axis and its distribution all agree with the figures above them
    // — plotting raw loss under a heading that says "success" drew the panel upside
    // down against its own numbers.
    tile: m => (100 - m).toFixed(0),
    toChart: v => 100 - v,
    caption: "last probe round",
    chartFmt: v => v.toFixed(1) + "% of probes answered",
    axisFmt: v => Math.round(v),
    floor: 0,
    ceiling: 100,
    explain: ["What is packet loss?",
      "The share of test packets that never came back, shown here the other way up as " +
      "the share that did. Steady loss above about 2% breaks calls and stalls downloads " +
      "even when latency looks fine, because every lost packet has to be noticed and " +
      "sent again."],
  },
};
