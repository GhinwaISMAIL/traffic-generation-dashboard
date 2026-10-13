# Demo result validation — 24 September 2026

The 900-second replay completed successfully. Packet counts, byte counts, and
latencies agree with the raw MGEN logs. The measurements describe a simulator
running substantially slower than real time; they do not establish a real-time
radio latency baseline.

## Execution

- Profile: `run_measurement_0565ccf7b5a64f749085_test900_audit`
- Execution: `mgen-20260924-125604`
- Schedule: 49 sessions, 7 traffic UEs, 6 applications; final session ends at 410 s.
- Observation: 900 s; the logical scenario remains 86,400 s.
- Radio capture: configured from traffic start for 450 s; usable samples cover
  approximately 6–457 s after traffic start, including monitor startup overhead.
- The original MGEN scripts were reused. No radio calibration values were changed.

The execution archive is under the profile's `executions/mgen-20260924-125604/`
directory in the traffic-generator repository. All 86 archived file checksums
verified successfully.

## Packet results

| Measurement | Downlink | Uplink |
| --- | ---: | ---: |
| Transmitted packets | 28,574 | 7,744 |
| Received packets | 28,574 | 7,744 |
| Packet loss | 0% | 0% |
| Received application bytes | 38,629,000 | 1,550,857 |
| Average throughput over 900 s | 0.343369 Mbps | 0.013785 Mbps |
| Packet latency p50 | 15.383 s | 0.505 s |
| Packet latency p95 | 76.568 s | 3.409 s |
| Maximum packet latency | 81.264 s | 5.129 s |

Independent parsing of the raw send/receive timestamps matched all 36,318 packet
identifiers and payload sizes. There were no duplicate send or receive
identifiers. The maximum difference from archived packet latency was
0.000237 ms, attributable to timestamp floating-point representation.

The dashboard now calculates packet percentiles rather than a median of flow
medians. Each received packet contributes equally to the percentile. Throughput
uses the entire execution duration, including idle time; it is not burst peak
throughput.

| UE | DL p50 (s) | DL p95 (s) | UL p50 (s) | UL p95 (s) |
| --- | ---: | ---: | ---: | ---: |
| ue1 | 52.628 | 79.497 | 1.238 | 3.283 |
| ue2 | 0.260 | 14.281 | 0.299 | 0.519 |
| ue3 | 8.619 | 35.473 | 0.375 | 1.621 |
| ue4 | 0.366 | 2.503 | 0.427 | 0.567 |
| ue5 | 3.685 | 10.103 | 0.480 | 1.755 |
| ue6 | 17.789 | 31.756 | 0.755 | 4.363 |
| ue7 | 0.506 | 3.764 | 0.435 | 0.672 |

Node clock checks passed before and after traffic. The measured offset spread
was 0.540 ms before traffic and 0.415 ms afterward. These small offsets cannot
explain delays of tens of seconds. UE identities and RNTI mappings remained
stable throughout the execution.

## Monitoring correction

The previous monitor recorded receipt time in the SQLite writer, after waiting
in the database queue. That could make report timing appear much slower and
produce a large apparent timestamp lag. Those earlier source/receipt ratios
must not be interpreted as simulator speed.

Receipt time is now captured before the queue, and the monitor uses the
supported 10 ms reporting interval. Each of MAC, RLC, PDCP, and GTP recorded
45,208 callbacks and 45,208 stored reports. All four subscriptions and delete
responses completed, with zero xApp errors and a clean shutdown.

The corrected source-to-recorded-time span ratio is approximately 1.000.
The saved per-second radio records have a mean source/receipt timestamp
difference of 1.54 ms. This difference includes clock and processing effects;
it is not an isolated E2 network latency measurement.

The archived `logs/radio_capture_quality.json` records the receipt timestamp
provenance, service counts, and frame progress. `logs/rlc_by_second.csv` preserves
buffer observations. These sidecars are part of the verified archive.

## Radio performance finding

An independent measurement from the gNB MAC frame counter shows 1,870 radio
frames advancing over 452.070 wall-clock seconds, without counter
discontinuities. That is 18.7 seconds of radio frame time, or **4.14% of real-time
speed**, approximately 24 times slower than real time.

UE1's recorded RLC transmit buffer reached 16,451,168 bytes. A 240-second CPU
sample showed the gNB `ru_thread` using approximately one full CPU core.
Twelve UE containers were attached, although this scenario sent application
traffic through seven. These observations support a radio processing bottleneck
and substantial queueing; they do not identify a specific function to optimize.

Zero loss and long delay are consistent: packets accumulated in queues and
eventually arrived. The packet results are useful for diagnosing this execution.
A real-time baseline requires addressing radio execution speed and repeating
the validation. Dividing the delays by the slowdown factor would not correct
them, because application traffic was paced in wall time.

This run did not capture verified live channel impairment settings, so it must
not be assigned noise/pathloss labels retroactively. Older executions and their
archives were preserved.

## Software checks

The release validation passed all 174 tests, bytecode compilation, and CLI
checks. The Results page was checked with this execution, including packet
percentiles, monitoring completeness, the shared traffic/radio time axis, and
the explicit warning about slow radio frame progress.
