---
date: '2026-05-20T10:00:00+08:00'
title: 'TENT Internal #3: Slice Spraying and QoS Mechanisms'
description: "Deep dive into TENT's dynamic scheduling algorithms, slice spraying technology, and QoS priority scheduling mechanisms"
comments: true
math: true
---

## 1. Slice Spraying Algorithm Design

In previous articles, we introduced TENT's architectural design and orchestrator core implementation. This article will deeply explore two dynamic scheduling mechanisms of TENT: **Slice Spraying** and **Quality of Service (QoS)**.

> **Series Navigation**:
> - **TENT Internal #1**: [Architecture Design Overview](/en/posts/tent-internal-arch/) - From imperative to declarative architecture evolution
> - **TENT Internal #2**: [Orchestrator Core Design](/en/posts/tent-internal-orchestrator-part-1/) - Late binding and path synthesis
> - **TENT Internal #3**: [Slice Spraying and QoS Mechanisms](/en/posts/tent-internal-slice-spraying-and-qos/) - Dynamic scheduling algorithms and performance optimization

### 1.1 Algorithm Background

In multi-tenant, high-concurrency distributed storage systems, how to efficiently utilize multi-rail RDMA network bandwidth while ensuring service quality for different priority requests is an extremely challenging problem. Mooncake adopts a simple Round-Robin strategy for allocation, which cannot optimize scheduling based on actual network conditions.

TENT addresses this pain point through the Slice Spraying mechanism. Its core idea is: **treat all rails as a unified resource pool, break data transmission into finer-grained slices, and dynamically schedule each slice based on a telemetry-aware cost model**.

![](/images/tent-internal-slice-spraying-and-qos/slice-spraying-motivation.png)

The above figure shows the problem we observed in our production environment: in round-robin mode, rails connected to remote NUMA domains exhibit significantly higher slice service times, and queue backlog on these specific rails directly pushes the overall request P99 latency higher.

### 1.2 Algorithm Evolution Ideas

The core idea of the Slice Spraying algorithm went through three stages of evolution:

**Stage One: Simple Round-Robin (Baseline)**
- Adopts static Round-Robin strategy for network bandwidth allocation
- Advantages: Simple implementation, zero overhead, excellent performance in ideal environments
- Limitations: Cannot perceive network state, performance limited when facing heterogeneous links and dynamic loads

**Stage Two: Dual-Parameter Model (Early Attempt)**
- Intelligent scheduling based on predicted transmission time
- Uses $\beta_0$, $\beta_1$ dual-parameter model for bandwidth estimation
- Limitations: Complex tuning, parameter sensitive, actual performance sometimes inferior to baseline

**Stage Three: EWMA Single-Parameter Model (Optimized Solution)**
- Simplified to single EWMA bandwidth estimation
- Allocated by device capacity ratio, fully utilizing multi-rail RDMA aggregate bandwidth
- Advantages: Simplified model, reduced state synchronization, enhanced scalability

### 1.3 Baseline Implementation

Mooncake Transfer Engine adopts simple round-robin design principles. In this scheme, device selection logic is very straightforward - the system maintains a local counter, and each time a new transfer request arrives, it sequentially selects the next device from the device list at the current NUMA level. Only when there are no available devices at the current NUMA level does it fall back to the remote NUMA level.

```cpp
// Simple round-robin strategy
thread_local int id = 0;
for (size_t rank = 0; rank < Topology::DevicePriorityRanks; ++rank) {
    auto& list = entry->device_list[rank];
    if (list.empty()) continue;
    chosen_dev_id = list[id % list.size()];
    id++;
    return Status::OK();
}
```

The core advantage of this design is determinism and predictability. Since there's no need to maintain any load state or perform complex calculations, the system's runtime overhead is nearly zero. In ideal environments where device performance is identical and load distribution is uniform, this scheme can perfectly complete the work.

However, its limitations are equally obvious. Faced with real-world complexity - heterogeneous link quality, dynamically changing load conditions, and latency differences caused by cross-NUMA access - this static round-robin strategy appears powerless. When certain links experience performance degradation due to congestion, the round-robin algorithm still sends the same number of requests to them, leading to reduced overall throughput. This can be seen from the previous figure.

### 1.4 Early Mooncake TENT Implementation

To address the baseline implementation's limitations, Mooncake TENT attempted to introduce an intelligent scheduling algorithm based on adaptive feedback. The core idea of this algorithm is to estimate the expected delivery time for each destination NIC by selecting a local NIC for delivery that is as fast as possible. Remote NICs follow principles similar to Mooncake TE, matching one-to-one when satisfying rank matching.

In this version, the expected delivery time for each local NIC is estimated as follows:
$$
predicted\_time = weight \times \left( \frac{inflight\_bytes + request\_bytes}{bandwidth} \times \beta_1 + \beta_0 \right)
$$
The algorithm uses a dual-parameter model to predict transmission time: $\beta_0$ represents fixed latency (such as PCIe transfer overhead, connection establishment time, etc.), $\beta_1$ represents the effective bandwidth correction coefficient. Weight is a constant; when the distance between local storage media and the NIC (NUMA, etc.) is longer, a higher coefficient reduces the probability of selecting that card. For each transfer request to be processed, the system calculates the predicted completion time for all candidate devices and selects the one with the best performance. The prediction formula comprehensively considers the device's current active load, theoretical bandwidth, and correction parameters learned from history.

To ensure parameters can adapt to constantly changing network conditions, the algorithm implements a sophisticated update mechanism. Each time a transfer completes, the system records the difference between actual time and predicted time, and uses exponential smoothing to update the $\beta_0$ and $\beta_1$ parameters corresponding to each NIC.

However, when we validated this model in practice, we found that this carefully designed system exposed some issues in actual operation, with performance in some cases even below baseline levels. First, the dual-parameter model increases tuning complexity - operators need to understand both the meaning of $\beta_0$ and $\beta_1$ and set appropriate thresholds separately to effectively adjust system behavior and avoid estimation distortion.

### 1.5 Optimized Mooncake TENT Implementation

After summarizing the gains and losses of the early implementation, we proposed an optimized solution that returns to fundamentals. The core idea of the new solution is: simplify the model, preserve the essence, and enhance scalability.

The expected delivery time for each local NIC is estimated as follows:
$$
predicted\_time = \frac{inflight\_bytes + request\_bytes}{ewma\_bandwidth} \times weight
$$
Note that we no longer use the $\beta_0$, $\beta_1$ dual-parameters that are prone to amplifying deviations. Instead, we simplify it to a single EWMA bandwidth estimation. Each device only needs to maintain one bandwidth estimate, and each time a transfer completes, the system updates this estimate based on the observed actual bandwidth. To prevent the estimate from deviating from a reasonable range, the system also sets boundary limits, constraining the EWMA value between 10% and 1000% of theoretical bandwidth. This conservative design ensures that even with abnormal observations, the system won't make overly extreme decisions.

Meanwhile, we specifically adopt a proportional allocation strategy for large requests. For requests with many slices, allocating them proportionally across available devices helps improve end-to-end bandwidth and reduce latency. Therefore, we don't use "each `Slice` independently decides the local NIC," but rather achieve full utilization of multi-rail RDMA aggregate bandwidth by allocating to multiple candidate devices proportionally. The proportion choice is mainly based on the previously measured $predicted\_time$. Devices with smaller $predicted\_time$ can occupy larger shares.

```cpp
// Allocate by device capacity ratio
double total_weight = sum(c.score for c in candidates);
for (const Candidate& c : candidates) {
    uint32_t allocation = (c.score / total_weight) * num_slices;
    for (uint32_t i = 0; i < allocation; ++i) {
        slice_dev_ids.push_back(c.dev_id);
    }
}
// Allocate remaining slices to optimal device
uint32_t remaining = num_slices - slice_dev_ids.size();
for (uint32_t i = 0; i < remaining; ++i) {
    slice_dev_ids.push_back(best.dev_id);
}
```

Overall, the optimized implementation achieves better balance between performance and maintainability by simplifying the model, reducing state synchronization overhead, and enhancing multi-path support.

## 2. Quality of Service (QoS) Mechanism
In multi-tenant, mixed-workload distributed storage environments, how to ensure critical operation response times while avoiding indefinite delays for background batch transfers is an extremely challenging problem.

### 2.1 Intra-instance QoS
To provide more predictable performance guarantees, TENT supports strict priority scheduling. In this mode, worker threads always process high-priority queues first, and only process medium-priority queues when the high-priority queue is empty, and so on. This design ensures that high-priority requests never wait for low-priority requests, thereby providing deterministic performance guarantees.

Strict priority scheduling implementation is very straightforward. In each scheduling cycle, the system checks each queue in priority order from high to low, and once it finds a non-empty queue, immediately processes the requests in it, then breaks out of the loop. This strategy guarantees that at any moment, if there is high-priority work waiting in the system, it will definitely get served first.

However, strict priority scheduling also brings a new problem: low-priority starvation. In scenarios where high-priority requests continuously arrive, requests in low-priority queues may never get execution opportunities. This is unacceptable for non-critical tasks like background batch transfers. Mooncake TENT introduces a timeout priority promotion mechanism. Each worker thread periodically checks each priority queue, and if it finds a request waiting time exceeding a configured threshold (default 10 milliseconds), it promotes it to a higher priority. Medium-priority requests waiting too long are promoted to the high-priority queue, while low-priority requests waiting too long are promoted to the medium-priority queue. This progressive promotion strategy ensures that all requests eventually get service even under high load conditions.

The implementation of this mechanism cleverly utilizes timestamp information. Each request records its enqueue time (`enqueue_ts`) when added to the queue. When checking promotion conditions, the system only needs to compare whether the difference between current time and enqueue time exceeds the threshold. To avoid frequent checking overhead, the system uses a simple optimization: records the next check time, and only executes the real check logic when that time is reached.

### 2.2 Multi-process QoS Coordination
In single-process environments, strict priority scheduling with anti-starvation mechanisms already works well. However, when the system runs multiple independent processes, each process only focuses on its own request scheduling, which may lead to a certain process's low-priority requests continuously occupying network bandwidth, affecting other processes' high-priority requests.

To solve this problem, Mooncake TENT designs a cross-process time-slot coordination mechanism. The system maintains a global time-slot state in shared memory, and all participating processes synchronize through this state. The time-slot mechanism divides time into fixed-length cycles, each containing three time slots:

- Time slot 0: Only allows high-priority requests
- Time slot 1: Allows medium-priority and high-priority requests
- Time slot 2: Allows all priority requests

The length of time slots is configurable, defaulting to 2 milliseconds, so the complete cycle length is 6 milliseconds. A background thread is responsible for periodically rotating time slots, ensuring all processes see the same current time slot value. Before processing requests, worker threads check whether the current time slot allows that request's priority, and skip processing if not allowed.

The benefits of this design are obvious. High-priority requests can get service in every time slot, but time slot 0 provides them with an exclusive execution window, ensuring the lowest latency. Medium-priority requests can get service in two-thirds of the time slots, while low-priority requests are only allowed to execute in one-third of the time. This balance that both guarantees service quality and avoids complete starvation is the core value of the time-slot mechanism.

Mooncake TENT's QoS mechanism provides rich configuration options, allowing operators to tune according to specific scenarios. For latency-sensitive critical applications, time slot intervals can be shortened to 1 millisecond, providing more frequent exclusive windows for high-priority requests. For throughput-oriented batch processing tasks, time slot intervals can be extended to 10 milliseconds, giving low-priority requests more execution time. Anti-starvation timeout settings also need adjustment based on application characteristics. Too short timeouts lead to frequent priority promotions, weakening the meaning of the priority mechanism; too long timeouts may cause low-priority requests to wait too long. The default 10 milliseconds is a compromise choice suitable for most general scenarios.

### 2.3 Performance Testing

![](/images/tent-internal-slice-spraying-and-qos/qos.png)

We evaluated TENT's QoS capability on an H20 testbed. The experiment uses two concurrent processes, each using 8 submission threads to fully utilize the $4 \times 400$ Gbps RoCE network. The first process initiates latency-sensitive "mice flows" (64 KB metadata synchronization, always set to high priority), while the second process generates heavy-throughput "elephant flows" (64 MB KVCache migration). We compared four configurations: No QoS, QoS (High+Medium), QoS (High+Low), and Solo single-task baseline. High priority intends to use priority time-slot rotation mechanism for transmission while actively suppressing low-priority traffic to maintain lower physical queue depth.

Results show that the P50 latency of mice flows decreased by 15.1%. This is mainly achieved through intentional time-slot throttling to alleviate inter-process competition. The implementation of this capability benefits from the engine's priority time-slot rotation mechanism - it tilts toward high-priority slices while ensuring overall transmission continues to advance, preventing low-priority traffic from starvation. Notably, P99 latency remained stable because when the device rotates to "accept low-priority" state, it receives all slices regardless of priority, ensuring high-priority intent is never indefinitely blocked by background traffic. These results confirm that TENT's QoS-aware Slice Spraying technology can effectively prioritize latency-sensitive critical mice flows (such as MoE expert parallelism traffic), directly contributing to reduced overall Time-To-First-Token (TTFT).
