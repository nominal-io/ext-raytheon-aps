# AMS GRA Hello World SK → Nominal Capture Matrix

Source of truth for outputs: `getting-started/docs/ports.md` (the port allocation
registry that `.env.example` is generated from), plus per-component configs.

Capture agent: `nominal-bridge` container on the sim's host network. Four Nominal
datasets: **DIS Ground Truth**, **UCI C2 Messages**, **Raw Sensor Streams**,
**Observability Metrics**, all attached to the *AMS GRA Hello World Starter Kit*
asset; each capture session is a Nominal run; state transitions become Nominal
events.


## At a glance

| Output | Captured? | How / why not |
|---|---|---|
| DIS EntityState multicast (5 Hz truth) | Yes | Per-entity position/velocity/attitude channels + events; raw PDUs logged verbatim |
| UCI via Sleet OWP (7 live message types) | Yes | Typed channels + state-transition events; full JSONL preserved |
| Squall IR frames (mono8 320x200 @ 8 fps) | Yes | Frame-stat channels + synced H.264 video channel; raw frame ring |
| Squall RF IQ (SC16 2.048 Msps @ 915 MHz) | Yes | Level/clip/spectral channels + spectrogram waterfall video; 2 s SC16 snapshots w/ sidecars |
| FM demod audio (S16LE 32 kHz) | Yes | Level/activity channels; WAV segments attached |
| Prometheus metrics (6 services) + Couloir gRPC status | Yes | Observability channels; sensor mode/LOS channels + events |
| 4 idle UCI topics + RoutePlan | Subscribed | Zero traffic in this kit; RoutePlan can't publish (kit's sleet auth omits the topic) |
| JSBSim consoles | No | Single-client FDM control link; tapping risks corrupting the sim; state arrives via DIS/UCI |
| FlightGear ctrl/FDM | No | Manned-flight option, inactive in default scenario; no packets exist |
| OTLP traces | No | Disabled by default; Nominal has no trace resource (span metrics come via Prometheus) |
| Graupel CZML | No | Rendering document; lossy re-encoding of DIS+UCI already captured at source |
| Worldview UI / tile server | No | Browser frontend; no telemetry content |
| Couloir commands / LA-CAL harness | No | Inputs and dev tooling, not outputs; effects visible in captured status channels |
| RTL-SDR / HackRF health ports | N/A | Hardware-in-the-loop only; auto-captured by the same scraper if HITL compose is used |

## Data-plane outputs

| Output | Port / Proto | Producer | Captured? | How it lands in Nominal |
|---|---|---|---|---|
| DIS EntityState PDUs (scene truth) | 239.1.2.3:21100 UDP multicast | supercell | ✅ | Bridge joins the multicast group, parses IEEE 1278.1 EntityState (144 B), converts ECEF→WGS84. Time series per entity (tag `entity`): lat/lon/alt, speed, ECEF velocity, Euler orientation, force ID; `dis.entities_active` gauge; entity-appeared events. **Raw preserved**: every PDU logged verbatim (ns timestamp + length + bytes) to rotating binary logs, attachable to the run for protocol-level replay. |
| Squall IR raw frames (mono8 MEL) | UDP, dynamic slot (documented default 21600) | squall-optical | ✅ | Bridge self-registers as a second data destination via Couloir gRPC `AddDataDestination` (no interference with the IR skill). Per-frame stats as time series: frame rate, mean/max/p99.9 intensity, saturated & bright pixel counts, target centroid x/y + pixel count. Latest frame snapshot saved as PNG for attachments. |
| Squall RF raw IQ (SC16 MEL, 2.048 Msps @ 915 MHz) | UDP, dynamic slot (documented default 21601) | squall-rf | ✅ | Same `AddDataDestination` tap. Time series: RMS/peak dBFS, clip fraction, packet rate, FFT peak offset & power (2 s cadence). Full-rate IQ itself is decimated to stats: 32 MB/s of raw samples is sensor-local data, not test telemetry. **Raw preserved**: 2 s SC16 snapshots every 5 min with JSON provenance sidecars (rate, fc, start ns, byte count), attachable to the run; plus a continuous 512-bin spectrogram waterfall (4 rows/s) rendered to an H.264 video channel `rf.video.spectrogram_915mhz`. |
| FM demod audio (S16LE PCM @ 32 kHz) | TCP 21602 | rf-fm-demod skill | ✅ | Bridge connects as TCP client. Time series: audio RMS/peak dBFS, audio-active flag. **Raw preserved**: rolling 2-minute WAV plus timestamped WAV segments every minute (last 12 kept), attachable to the run (the Artemis II go/no-go poll loop). |
| UCI `PositionReport` | ws 21402 (Sleet OWP) | supercell | ✅ | Bridge INITs as an authorized `nominal-bridge` OWP service (services.d grant) and subscribes. Perceived position/velocity time series tagged by entity — plotted against DIS truth. **Raw preserved** (all UCI topics): every message logged verbatim as JSONL with receive timestamps to rotating logs, attachable to the run. |
| UCI `ObservationMeasurementReport` | ws 21402 | ir-search-and-track | ✅ | LOS azimuth/elevation time series, measurements-per-report; IR track acquired/lost events (5 s gap detection). |
| UCI `SignalReport` | ws 21402 | rf-fm-demod | ✅ | Signal count, frequency, state, collector position time series; FM signal acquired/lost events (10 s gap detection). |
| UCI `NavigationReport` | ws 21402 | supercell | ✅ | Nav source / contingency-level string channels. |
| UCI `SubsystemStatus` | ws 21402 | squall OMS adapters | ✅ | Subsystem state string channel (tag `subsystem`); state-transition events. |
| UCI `ServiceStatus` | ws 21402 | all skills + adapters | ✅ | Service state string channel + uptime seconds (tag `service`); NORMAL↔DEGRADED transition events. |
| UCI `SystemStatus` | ws 21402 | supercell | ✅ | System state string channel; transition events. |
| UCI `ESM_Activity`, `PO_Activity`, `OperatorNotification`, `PositionReportDetailed` | ws 21402 | *(no publisher in this kit version)* | ✅ subscribed | Bridge subscribes and lands them via a generic flattener + point events the moment any future component publishes them. Zero traffic today. |
| UCI `RoutePlan` | ws 21402 | supercell *(code path exists, never fires)* | ✅ subscribed | Supercell's OWP task builds RoutePlan messages when its shared state has waypoints, but zero were published in 90+ min of live capture, and the kit's shipped `services.d/supercell.toml` doesn't even authorize `mission.route-plan` - a kit config inconsistency worth reporting upstream. Bridge subscribes anyway so a future kit version is captured automatically. |

## Telemetry / observability outputs

| Output | Port | Captured? | How |
|---|---|---|---|
| Supercell Prometheus metrics | 21300 HTTP | ✅ | Scraped every 5 s → `obs.*` channels tagged `service=supercell` (+ metric labels as tags). |
| Sleet Prometheus metrics | 21312 HTTP | ✅ | Same. Includes per-topic publish counters, delivery/drop counters, validation failures, connection gauge — the C2 health story. |
| Squall RF / Optical / Couloir metrics | 21314 / 21316 / 21318 HTTP | ✅ | Same, tagged per service. |
| Graupel health + metrics | 21401 HTTP | ✅ | Same. |
| Squall RF/Optical health checks | 21313 / 21315 HTTP | ✅ (indirect) | Liveness captured as `obs.scrape_up` per service + `squall.*.running` from gRPC GetStatus; the bare `/health` JSON adds nothing beyond that. |
| Squall control-plane status (mode, LOS pointing, platform attitude, fc, fs) | gRPC 21203 via Couloir | ✅ | Polled at 0.5 Hz → `squall.ir.*` / `squall.rf.*` channels incl. LOS az/el and platform yaw/pitch/roll; mode-change events. |
| RTL-SDR / HackRF health & metrics | 21319–21322 HTTP | ➖ N/A | Hardware-in-the-loop variants; those services don't run in the simulated deployment (no process bound). Captured automatically by the same scraper if a HITL compose is used. |
| Central Prometheus / Grafana | 21317 / 21323 | ➖ Not needed | Aggregation/visualization layer, not a data producer — Nominal replaces it (we scrape the sources directly). |
| Supercell OTLP trace export | 21310 gRPC / 21311 HTTP | ❌ | Disabled by default (requires `otlp` build feature + config). Also a poor fit: distributed *traces* are span trees, not time series/events — Nominal has no native trace resource. Span rates/durations would arrive via Prometheus metrics instead. |

## Control-plane / internal interfaces (not test data)

| Interface | Port | Captured? | Why not |
|---|---|---|---|
| JSBSim consoles (Eagle-1, Bandit-1, Bandit-2) | TCP 21110/21120/21121 | ❌ | Input/control sockets owned by supercell (its "Remote" FDM link). Attaching a second console client risks corrupting the FDM session; the same state arrives safely via DIS + UCI PositionReports. |
| FlightGear controls in / FDM out | UDP 21201/21202 | ❌ | Optional manned-flight (HITL) channel, inactive in the default scenario config — no packets exist to capture. If enabled, the bridge could bind 21202 and decode FGNetFDM for pilot-input telemetry. |
| Couloir gRPC command surface (TuneRf, CommandIr, …) | TCP 21203 | ➖ | It's a command *input*, not an output; the bridge uses it read-only (GetStatus, AddDataDestination). Command effects are visible in the captured status/mode channels. |
| Sleet OWP client port (as an input) | TCP 21402 | ➖ | Same port as the UCI capture above — listed here because it's also the system's command input; PUBs from any future C2 client are captured via the same subscriptions. |
| LA-CAL test harness | TCP 21403 | ➖ N/A | Dev-time conformance harness, not part of the running mission system. |
| Graupel CZML stream | ws 21400 | ❌ | CZML is a CesiumJS *rendering* document — a derived, lossy re-encoding of the DIS + UCI data we already capture at the source. Ingesting it would duplicate geometry as blobs Nominal can't decompose into channels. |
| Worldview UI / tile server | 21500/21501 HTTP | ➖ N/A | Browser frontend + map tiles; no telemetry content. |

## Data types Nominal can't ingest natively (and the workaround used)

| Stream | Native type | Limitation | Workaround |
|---|---|---|---|
| IR mono8 video (64 kB frames @ 8 fps) | Raw video | Nominal ingests encoded video (e.g. H.264/MP4), not raw sensor frame streams over UDP | Frame-statistics channels in real time + PNG snapshots; frames can be transcoded post-run into an MP4 and attached to the run as Nominal video |
| RF IQ @ 2.048 Msps (32 MB/s) | Wideband complex samples | Point-per-sample ingest at 4M points/s is neither practical nor useful for test review | DSP-derived channels (power, clipping, spectral peak) at review-friendly rates; raw IQ snippets could be attached as files if needed |
| PCM audio | Audio | No native audio channel type | Level/activity channels + WAV attachment on the run |
| OTLP traces | Distributed trace spans | No trace resource in Nominal | Span-derived Prometheus metrics via the scraper |
