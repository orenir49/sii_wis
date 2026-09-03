# Network topology: the 2-node bottleneck, its root cause, and scaling to dozens of nodes

## Context

During the live A/B/C wire-encoding confirmation
(`docs/raw_timestamp_wire_encoding_bakeoff.md`, 2026-09-03), both nodes
started falling badly behind the detector at rates far below anything the
offline bake-off bench (`tools/bench_wire_encoding.py`) had predicted —
node1 in particular, repeatedly, across every wire mode. This doc records
the diagnostic path, the confirmed root cause, the fix adopted for now, and
a topology recommendation for when this system needs to scale from 2 nodes
to dozens.

## Symptom

At ~2 Mcps/pixel (post optical-realignment), a 2-pixel identity-pair live
run (baseline mode, `pair 164×164` + a second pixel) showed:

| | node1 | node2 |
|---|---|---|
| records | 627,105,634 | 638,980,053 |
| queue peak | **200/200 (FULL)** | 2/200 |
| blocked | **1,831×** | 0 |
| parser lag (peak) | **55.0 s** | 8.1 s |

Both nodes saw almost identical raw event counts, yet node1's send queue
saturated completely while node2's barely moved — immediately ruling out
"aggregate rate exceeds the parser's CPU ceiling" as the *sole* story: if
that were true both nodes, running the same code on comparable hardware,
should have failed similarly. The `node_backend.run()` log itself flagged
the actual mechanism: *"the parser was stalled waiting on the receiver, so
any FIFO overflow above was caused downstream of the detector, not by it"*
— node1's own parsing was not the limit; something downstream of it (the
network, or the master's receive side) was.

## Hypotheses tried, in order, and why each was ruled out

1. **Rate too high for the post-Phase-1 parser ceiling.** Cutting the mask
   from 5 → 2 → 1 active pixel eventually gave clean runs (1 pixel, ~2.7
   Mcps/node, 0.0 s lag on both nodes, all three wire modes). But 2 pixels
   (~5.2 Mcps/node — barely 2×) collapsed catastrophically rather than
   showing gradual degradation, which is the wrong shape for a smooth
   CPU-bound ceiling.
2. **Windows Defender real-time scanning** (no exclusions existed for the
   `sii_wis` directory, `lSPAD.exe`, or `python.exe`/`pythonw.exe` on
   either node, or for the master). Added exclusions on all three
   machines. Node2 then ran a 20-pixel case (~4.6 Mcps) completely clean —
   a real improvement. Node1 still failed on the *same* mask, at a *lower*
   observed throughput (468.6M records) than node2 had just carried
   cleanly (559.1M) — Defender was a real contributor for node2 but not
   the story for node1.
3. **Node1-specific hardware (CPU tier, thermal throttling).** Node1 is
   actually the higher-tier part (Core Ultra 7 155H, 16 cores) versus
   node2's 155U (12 cores) — if anything it should be faster. Both
   machines are on AC power (not battery-throttled). Live-sampled node1's
   CPU utilization and `% Processor Performance` (current clock vs base;
   >100% means turbo, not throttling) throughout a failing-scale run:
   utilization stayed low with turbo consistently engaged even under
   sustained load in a later isolated test (see below) — no CPU or
   thermal ceiling was ever actually reached.

## Root cause, confirmed

**Both nodes' USB 2.5GbE adapters connect to a single unmanaged 1GbE
switch, which uplinks to the master's native 2.5GbE port over one cable.**
Node1 and node2 do not have independent paths to the master — they share
one physical link and contend for it whenever both stream at meaningful
rates simultaneously.

**Decisive test**: with node2 fully disconnected, node1 alone ran the
*exact* 20-pixel mask/rate that had just failed with node2 also connected:

```
[N1] Done. Elapsed: 120.7 s — 474,088,129 records, 0 overflow,
     lag 0.1 s (peak 0.1 s), queue peak 5/200
```

Completely clean — actually carrying *more* throughput than it had
managed while contending with node2. CPU sampled throughout: 5-19%
utilization, 120-265% of base clock (strong turbo, zero throttling), the
whole run. This rules out every node1-specific hypothesis and confirms the
shared uplink is the actual constraint.

**Bandwidth accounting** (baseline mode, 8 B/event on the wire):

| condition | combined throughput | result |
|---|---|---|
| 1 pixel/node (~2.7 Mcps each) | ~344 Mbps | clean, both nodes |
| 20 pixels/node (~4.6-4.7 Mcps each) | ~550-600 Mbps | node1 collapses |

Well under the switch's nominal 1 Gbps rating by raw arithmetic, which is
exactly what a cheap unmanaged switch's *real* sustained simultaneous
bidirectional throughput commonly fails to deliver — nominal port speed is
not a promise of non-blocking aggregate throughput, especially with two
USB-based adapters (inherently less predictable under load than native
NICs) sharing one uplink.

**Practical working ceiling on the current (shared-switch) wiring**: keep
combined throughput comfortably under ~400 Mbps until the wiring below is
in place.

## Fix adopted now: dedicated per-node links

Each node gets its own physical cable directly into the master, bypassing
the shared switch entirely — the master needs one dedicated port per node
(a second USB-Ethernet adapter, in addition to its native port, covers the
2-node case). This removes the contention completely rather than managing
around it, and costs nothing in the software stack.

## Topology for the future: dozens of nodes into one master

A dedicated physical port per node does not scale past a handful of
nodes — no PC has dozens of Ethernet ports, and adding USB adapters one at
a time is not a real architecture. For real growth, in order of
increasing scale/complexity:

### 1. Managed switch, sized uplink, one master NIC

Replace the unmanaged 1GbE switch with a **managed** switch: every node
still gets its own dedicated access port (switches already give each
access port full bandwidth to the backplane — that part of a star
topology was never the problem), but the **uplink to the master must be
sized for realistic simultaneous aggregate demand**, not left at whatever
a random single port happens to offer. Concretely: estimate
(active-pixel-count × Mcps × bytes/event) summed over every node expected
to stream *at the same time*, size the uplink (and the master's own NIC)
with real headroom above that — a 10GbE uplink and matching 10GbE NIC on
the master comfortably covers many nodes at today's per-node rates. This
is the minimum credible fix and is enough for perhaps a dozen
simultaneously-active nodes before the next tier is needed.

Managed (not unmanaged) also buys **QoS**: put each node's control-channel
traffic (JSON START/STOP/abort commands, log forwarding) in a
higher-priority class than the bulk data channel, on a separate VLAN if
the switch supports it. At today's 2-node scale a saturated data channel
only delays that node's own photons; at dozens-of-nodes scale, a control
command (an ABORT, say) getting stuck behind someone else's bulk transfer
on a shared, non-prioritized link is a real operational hazard.

### 2. Multiple master NICs, node groups split across them

One NIC (even 10GbE) is still one finite pipe and one point of failure.
Once aggregate demand from simultaneously-active nodes approaches what a
single master NIC can sustain, split nodes into groups, each group's
switch (or switch uplink) landing on a **different physical NIC** on the
master. This scales linearly with the number of NICs added and avoids the
current failure's exact shape (two things sharing one physical path)
recurring at ten times the scale. `run_session_loop` already runs one
thread per data connection, so the software side needs no change — this
is purely a wiring/NIC decision.

### 3. Hierarchical aggregation, if dozens must stream at full rate at once

If the real requirement becomes dozens of nodes *simultaneously* streaming
at meaningful rates (not just dozens of nodes existing, with only a few
active in any one session, which is the more likely case given how masks
are used today), a flat star — however well-provisioned — eventually
hits the master's own CPU/memory ceiling for receiving, decoding, and
correlating that many concurrent streams in one process. The standard
answer in large distributed DAQ systems is a **tree**: small groups of
nodes (4-8) feed a local concentrator that does first-stage buffering
(and, per the wire-encoding bake-off, could re-encode with delta
compression here — see below) before forwarding one aggregated,
already-reduced stream up to the real master. This is a genuine
architecture change (new concentrator-tier software, not just wiring) and
should only be built if profiling at the Tier-1/Tier-2 scale above
actually shows the master process itself, not the network, as the next
constraint.

### Bandwidth compression is now directly relevant at scale

The wire-encoding bake-off (`docs/raw_timestamp_wire_encoding_bakeoff.md`)
framed raw-column vs. delta-encoding primarily as a **node-CPU** cost
question, and on that framing delta-encoding lost (higher node elapsed
time, live-confirmed). But today's finding is that the actually-scarce
resource in this deployment is **network bandwidth**, not node CPU —
delta-encoding's ~2.00× wire compression (confirmed exactly on real data,
both offline and live, this session) directly relieves the resource that
just caused a real outage, in a way raw columns' CPU-side savings do not.
Worth revisiting if the dozens-of-nodes topology above still runs the
shared/oversubscribed-uplink tier of this plan rather than a fully
per-node/per-NIC-provisioned one.
