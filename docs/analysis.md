# How netblame decides who is to blame

## 1. Sampling

Every `interval_s` a *tick* fires and one `ping -n 1 -w <timeout>` is started
for **every target at the same moment** (one thread per ping). All samples that
share a tick were taken at the same time. That shared timeline is what the rest
of the analysis is built on.

Parsing is locale-tolerant. A reply is recognised by its round-trip time
(`time=12ms`, `time<1ms`, Dutch `tijd=12 ms`, ...). `time<1ms` is recorded as
0.5 ms. "Destination host unreachable" counts as lost, even though Windows
calls it a "reply".

## 2. Per-target statistics

For each target:

| Metric | Definition |
|---|---|
| loss % | lost / sent |
| p50, p95, max | median, 95th percentile and maximum RTT of the replies |
| jitter | mean absolute difference between consecutive RTTs |
| bad sample | a lost ping **or** a reply slower than `spike_ms` |
| incident | `burst_min_samples`+ consecutive bad samples |

Each target is graded **OK / WARN / BAD** against the thresholds of its kind.
The worst of the loss, p95 and jitter checks decides the grade.

### Default thresholds

| kind | loss warn/bad | p95 warn/bad | jitter warn/bad | spike |
|---|---|---|---|---|
| `lan` | 0.5 / 2 % | 20 / 50 ms | 8 / 20 ms | 150 ms |
| `isp_gateway` | 0.5 / 2 % | 25 / 60 ms | 8 / 20 ms | 150 ms |
| `internet` | 1 / 3 % | 60 / 150 ms | 15 / 40 ms | 300 ms |

Rationale:

- **LAN / gateway**: a healthy wired hop answers in under 1–2 ms, and good Wi-Fi in a
  few ms. Occasional Wi-Fi bumps to 10–20 ms are normal, so warnings start at
  20 ms p95. Any sustained loss on a local hop is abnormal, so 2 % is already bad.
- **Internet**: from a Belgian/European broadband line, 8.8.8.8 and 1.1.1.1
  are normally 8–25 ms away. A p95 of 60 ms means noticeable congestion, and
  150 ms makes video calls and gaming suffer. Real-time traffic (VoIP, games)
  degrades from about 1 % loss and becomes painful above 3 %.

Override any of them per kind in `config/netblame.yaml`.

## 3. Hops (levels) and correlation

Targets are grouped by `level`: `lan` = 1, `isp_gateway` = 2, `internet` = 3.
You can override this per target. Targets on the same level are treated as one hop:

- A **hop is bad at a tick** only if *all* its targets are bad at that tick.
  If 8.8.8.8 drops while 1.1.1.1 answers, that is route-specific, not your line.
- A **hop's grade** is the *best* grade among its targets, for the same reason.

For every hop, its bad ticks are split into:

- **own**: bad while every closer hop was fine. The problem starts here.
- **knock-on**: bad while a closer hop was also bad. Inherited trouble.

## 4. Verdict rules

Walking the hops from the PC outwards:

1. A hop graded OK is not blamed.
2. **ICMP deprioritisation**: if a hop looks bad but *every* farther hop is
   healthy, traffic through that device is clearly fine. Routers often
   rate-limit pings aimed at themselves, so this becomes a note, not blame.
3. **Knock-on**: if at least 60 % of a hop's bad moments coincide with
   problems on closer hops, it is not blamed separately. If nothing closer has
   been blamed yet, the closer hop whose hiccups overlap most is blamed instead,
   even if it stayed under its own thresholds.
4. Otherwise the hop is blamed. Its segment is "between the previous hop and
   this hop", e.g. *the ISP line / upstream network (between Telenet HGW and
   Google DNS)*.

The first blamed segment is the primary culprit. More than one blamed
segment means several independent problems.

Extra hints:

- Internet hop blamed and replies took **over 1 second** while still
  arriving: packets are queueing, not just vanishing. That points to upstream
  congestion or your own upload saturating the line (bufferbloat), so the
  report suggests pausing big uploads and re-running.
- Healthy runs shorter than 5 minutes get a suggestion to run longer while
  the problem is happening.

## 5. Limitations

- Ping (ICMP) is a proxy for real traffic. Some devices and ISPs deprioritise it,
  which rule 2 accounts for but cannot rule out completely.
- A run only proves what happened *during* that run. For intermittent problems,
  run longer (`-d 15m`, `-d 1h`) while the symptoms are present.
- With one internet target, "that destination is bad" cannot be told apart
  from "my line is bad". Configure at least two.

## 6. `--explain`

netblame does not call any LLM. `--explain` builds a prompt from the computed
summary (`Analysis.to_dict()`: statistics, hop results, verdict, evidence,
incidents; no raw samples) plus instructions. The instructions tell the model
how to read the data, not to contradict the verdict, and to draft an ISP message
when the upstream network is blamed. The prompt is printed and copied to the
clipboard (`clip`), or written to a file with `--explain-file`. Paste it into
any LLM. The verdict never depends on it.
