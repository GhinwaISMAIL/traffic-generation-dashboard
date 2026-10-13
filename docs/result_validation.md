# Result validation

Packet delivery, monitoring integrity, and radio execution speed are separate
checks. A completed deployment alone does not establish a real-time latency
baseline.

## Reading the Results page

The overview separates packet matching, node clock checks, monitoring
completeness, and radio execution speed. Missing evidence is labelled
unverified. The simulator-speed warning remains visible above every tab.

Traffic uses paired downlink/uplink bars on one scale. The timeline compares
transmitted and received payload in selectable intervals; its zeros mean no
logged packets, not proof of complete log collection. The execution average
always includes idle seconds. Logical scenario duration and execution duration
are shown separately.

Delivery shows planned, transmitted, and received packet counts. Missing planned
data is omitted rather than plotted as zero. Delay cards and bars separate the
two directions, and long delays use seconds. Zero loss is labelled explicitly.

Help icons provide metric definitions on hover or keyboard focus. The same
definitions are available under Data & methods. CSV summaries and selected
chart PDFs can be downloaded there; PDF rendering starts on download rather
than on every page refresh.

## Packet measurements

The Results page computes p50 and p95 latency directly from matched transmitted
and received packets, grouped by UE and direction. Each received packet has one
vote. A median of flow medians gives small and large flows equal weight and can
hide delays affecting most packets; it remains an explicitly labelled fallback
when packet reconstruction is unavailable.

The charts, flow summaries, and packet exports share one matched packet table.
Receive duplicates count once for delivery, throughput, and latency. Duplicate
send identifiers, unknown received identifiers, missing identifiers or payload
sizes, and payload-size mismatches are rejected. Negative or missing delays are
excluded from percentiles with an explicit warning. Cross-node latency still
requires synchronized node clocks. Archived packet exports retain clock-validity
flags and do not substitute time-of-day fallback for UTC latency.

Throughput is received application payload bytes multiplied by eight and divided
by the configured execution duration. Idle seconds contribute to that duration.
Burst peaks can therefore be much higher than the run average. Zero packet loss
does not imply low delay: a queue can eventually deliver every packet.

The displayed PRB rate divides each counter increment by its actual receipt
interval. The application bits/PRB diagnostic still joins approximately
one-second UTC buckets. It is not a causal assignment of individual received
packets to the radio resources that carried them.

## Monitoring timestamps

The core records receipt time before placing each decoded indication into the
database queue. Recording that time in the SQLite writer instead measures when
storage catches up and can misalign radio and traffic events.

`bin/patch-monitor-receipt-time.py` prepares a checked patch for the installed
FlexRIC source version. It retains the existing three-argument database writer
interface and adds a timestamp-carrying entry point for queued indications. The
monitor uses the supported 10 ms reporting interval and prints per-service
callback totals after shutdown. Installation and compilation are separate from
patch preparation; preserve the original source and executable before applying
it to the monitoring host.

`bin/aggregate-radio.py` produces per-second PRB observations, an RLC buffer
occupancy sidecar, and a capture-quality sidecar. With the new monitor, it checks
that the number of distinct stored reports equals the callback count for each
service. Multiple UE rows belonging to the same report count as one report.
Incomplete captures fail aggregation and retain the source database.

The runner saves these sidecars as `logs/rlc_by_second.csv` and
`logs/radio_capture_quality.json`. Execution archives preserve them. Historical
archives are unchanged; a missing receipt-time marker is displayed as unverified
timestamp provenance.

## Radio execution speed

The source-to-receipt timestamp span ratio checks report timing. It is not a
measurement of simulator speed. A database backlog can make that ratio small
even when report timestamps follow wall time.

Radio frame progress is measured independently from the gNB MAC frame counter:
unwrapped frame count times 0.01 seconds, divided by elapsed wall time. The
counter wraps at 1024. Backward discontinuities and observation gaps long enough
to make unwrapping ambiguous invalidate the estimate. A ratio near one means
frame time advances near wall time. Slower progress means measured packet delays
include the simulator's execution speed and should not be presented as a
real-time radio latency baseline.

## Selecting the capture window

Choose the xApp delay and window under the Testbed page's advanced distributed
settings. Cover both the offered burst and the period in which its queued data
arrives. For the 900-second demo with sessions ending at 410 seconds, the
diagnostic replay uses a delay of zero and a 450-second window. Longer traffic
or a longer queue drain requires a longer capture.

Radio and traffic chart times use the saved traffic-start epoch when present.
The Results page reports the actual usable radio interval; missing samples are
not interpreted as zero resource use.
