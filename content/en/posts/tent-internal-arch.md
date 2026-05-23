---
date: '2026-04-24T15:00:00+08:00'
title: 'TENT Internal #1: Architecture Design Overview'
description: "Deep dive into how TENT evolves from imperative interfaces to declarative orchestration architecture, solving communication challenges in heterogeneous interconnect environments"
comments: true
math: true
---

> **Series Navigation**:
> - **TENT Internal #1**: [Architecture Design Overview](/en/posts/tent-internal-arch/) - From imperative to declarative architecture evolution
> - **TENT Internal #2**: [Orchestrator Core Design](/en/posts/tent-internal-orchestrator-part-1/) - Late binding and path synthesis
> - **TENT Internal #3**: [Slice Spraying and QoS Mechanisms](/en/posts/tent-internal-slice-spraying-and-qos/) - Dynamic scheduling algorithms and performance optimization

## 1. Introduction: Interconnect Challenges in Modern AI Clusters

I have written many presentations reviewing the development of AI Stor:

- **2024** was the first year of P/D (Prefill/Decode) separation. Through physical or logical decoupling of compute-intensive prefill phases and memory-intensive generation phases, we initially addressed the inherent contradiction between inference throughput and latency. **This year, Mooncake grew from an arXiv paper into a runnable open-source project.**

- **2025** was the year of large-scale KVCache adoption. The industry established a "storage-for-compute" paradigm through storage disaggregation architectures. KVCache evolved from a disposable intermediate variable to a core state asset driving inference services. **This year, Mooncake expanded from simple vLLM adaptation to covering multiple inference frameworks, GPU models, and application scenarios.**

- **2026** is the explosion of agents. I believe everyone has seen in these four months that large models are evolving from pure semantic generation engines to "reasoning hubs" with long-range planning capabilities, driving longer sequences and higher-frequency task loops.

As a Storage for AI researcher, how do I view this problem?

### 1.1 State Assets: Beyond KVCache

KVCache has evolved from transient data to cross-stage reusable core storage assets. For example, the standard "context memory" in current LLM inference largely benefits from pooled KVCache storage. Additionally, KVCache achieves high cache hit rates in multi-turn conversations and AI-assisted programming scenarios.

> Take Kimi (Moonshot AI) as an example. The KVCache hit rate in their production environment has reached 90%. This极致的复用效率 directly reduces users' actual billing costs to 25% of market standard prices, making it extremely competitive commercially.

However, in 2026's agent paradigm, "state assets" are taking more complex forms:

- **Model weight updates in online RL pipelines.** According to Kimi Team's technical report, online RL requires microsecond-precision synchronization of gradients or weights of hundreds of billions of parameters during model sampling (Rollout) to avoid sampling tasks stalling while waiting for model refresh. In Moonshot Checkpoint Engine's typical scenarios, these frequent parameter distributions manifest as extremely high-density "full sync elephant flows."

- **Expert Parallelism (EP) in MoE (Mixture of Experts) models.** In MoE architectures, tokens need to frequently shuttle between different experts. This expert parallelism traffic (mice flows) is typically only tens of KB but lies on the inference execution critical path.

### 1.2 Interconnect Links: Beyond RDMA

From a link perspective, storage media with vastly different physical characteristics (DRAM, GPU/NPU memory, NVMe SSD, etc.) and various high-speed interconnect technologies (NVLink, RDMA, CXL, etc.) together form a complex heterogeneous storage interconnect system.

![](/images/tent-internal-arch/ai-cluster-topology.png)

Let me illustrate with a typical hyperscaler topology. Each inference server has 8 GPUs connected via NVLink or Ascend UB and other scale-up high-speed interconnect protocols, with bandwidth typically reaching hundreds of GB/s. For NVIDIA GB200 NVL72 hyperscaler clusters, NVLink can span inference servers, achieving all-GPU interconnect within a rack. Sometimes CXL can also serve as scale-up networking. So this is essentially intra-rack networking.

To implement efficient data planes across inference servers, each server is equipped with several RDMA NICs, forming a complete scale-out RDMA network through two-tier switches. To maximize aggregated bandwidth, typically 4-8 cards are installed, achieving up to $8 \times 400$ Gbps aggregated bandwidth. Generally, this network exists within a physical data center.

Interestingly, boundaries sometimes span data centers. For example, PrfaaS (media calls it "pre-made computing") recently proposed by Kimi and our team involves running some Prefill tasks and Decode tasks in two data centers connected only by Ethernet dedicated lines.

![](/images/tent-internal-arch/prfaas.png)

> Reference: [Prefill-as-a-Service: KVCache of Next-Generation Models Could Go Cross-Datacenter](https://arxiv.org/pdf/2604.15039)

## 2. Limitations of Existing Frameworks: The Dilemma of Imperative Interfaces

### 2.1 Mooncake TE Looks Good?

I believe everyone has learned about Mooncake TE's architecture through various channels. Its architecture is actually quite simple, stemming from a chain of early design philosophy.

Initially, Mooncake's core design was heavily influenced by my PhD research work SMART (ASPLOS 24). In fact, I only did one thing: copy & paste code from SMART, modifying it from single-NIC to multi-NIC usage (including that frequently problematic Handshake module). That was old-school programming. Obviously, that was a purely RDMA-centric world: we assumed physical topology was relatively pure, communication logic was imperative, and as long as we managed RDMA resources well, performance was excellent. Although there was TCP, that was purely to meet the needs of platforms without RDMA.

However, we later discovered that real-world environment evolution was far more complex than lab expectations:

1. **The rise of NVLink:** It introduced completely different memory space management and IPC handle mechanisms from RDMA. In some advanced hyperscaler servers we encountered, there wasn't even RDMA connection between two machines, only NVLink connection!

2. **Proliferation of domestic platforms:** From the first domestic platform Ascend (and its HIXL framework) to later adding more domestic brands, each has its own transport protocol and API.

3. **Strange application scenarios:** Initially we only did 1p1d, but later KVCache pooling, checkpointing, MoE EP, etc. were all used, each with different usage patterns.

Consequently:

- We found the Transport directory getting bloated. More and more duplicate code appeared. Each new platform addition meant manually adapting a new set of "jargon," with code filled with platform-specific conditional compilation branches. The originally clean Mooncake TE gradually became a "monster" forcibly stitched together by various backends, with rising maintenance costs and extreme fragility when facing complex failures.

- Interoperability got worse. Existing frameworks tightly bound transmission metadata with specific backends. This "endpoint-centric" architecture treated interconnects as isolated territories rather than a fungible resource pool. For example, putting NVIDIA GPUs and domestic chips in one transmission cluster, or using devices like GB200 NVL72 for remote DRAM access (actually not directly supported), could lead to crashes.

- Additionally, this brought "torture" to operators (or rather, gave them opportunities to report achievements to +1). In large-scale clusters, hardware failures are the norm, from NIC link flaps to bad cards (we later discovered bad cards have high probability), which is disastrous in the agent era requiring continuous availability. Mooncake TE somewhat tends to propagate failures across the entire cluster.

Actually, this series of problems is fundamentally a design paradigm issue. We call it the imperative interface. Simply put, it's the direct API translation of underlying links (even with some encapsulation to make different links' APIs look similar).

Take NIXL as an example:

```cpp
// ❌ NIXL style: developers still need to intervene in resource & backend binding
// 1. Create agent and specific backend plugin (explicitly specify UCX, for example)
NIXLAgent* agent = nixlCreateAgent("node_0");
NIXLBackend* ucx_be = nixlCreateBackend(agent, "UCX"); // Explicitly bind to specific transport technology

// 2. Register segment (Segment), note: NIXL's segments are bound to specific backends
NIXLSegment* seg = nixlCreateSegment(agent, gpu_ptr, size, ucx_be);

// 3. Get and exchange metadata (Metadata)
// Developers often need to handle passing this MD to remote nodes themselves, or use NIXL's complex caching mechanism to manage it
NIXLMetadata md = nixlGetSegmentMetadata(seg);

// 4. Create and post request
NIXLXferReq* req = nixlCreateXferReq(agent, src_list, dst_list);
nixlPostXferReq(req); // Once selected backend (like UCX) experiences congestion at runtime, application layer struggles to intervene in dynamic path switching
```

> (NIXL skipped the class—it doesn't need to do Handshake! God knows how many pits Mooncake fell into in Handshake)

Obviously, in the above API, `agent`, `backend`, `segment`, `metadata` and other elements are tightly bound relationships, all depending on UCX as the unified transport backend for action. So if I want to read data from SSD, I need to allocate another set of objects—the two absolutely cannot be mixed. So, NIXL itself must bind to specific transport backends.

One might ask, doesn't UCX support mixing NVLink and RDMA? Indeed, UCX's "unification" mainly manifests in the API abstraction provided by the UCP (Unified Communication Protocol) layer, but at the underlying UCT (Unified Communication Transport) implementation, vendor barriers remain solid.

- **At compile time.** UCX decides at compilation whether to activate CUDA or ROCm support. Since NVIDIA's `libcuda.so` and AMD's `libhip_hcc.so` (or related runtime libraries) are completely independent in symbol naming, memory mapping logic, etc., production environment's `libucp.so` is often built for a single ecosystem. For domestic accelerator cards (like Huawei Ascend, Cambricon, Moore Threads, etc.), users often need to use vendor-maintained UCX private branches, further exacerbating serious version fragmentation.

- **At runtime.** NVIDIA uses `cudaIpcMemHandle_t` for cross-process memory sharing, while AMD uses its proprietary `hipIpcMemHandle_t`. These handles are essentially private metadata encapsulated by vendor drivers. UCX just provides this metadata to users as-is, without "translation" capability. Worst case, this causes CUDA runtime to initiate requests to AMD runtime, leading to direct downgrade to TCP or even crashes. To avoid this, we had to allocate multiple Agent sets to satisfy different target access needs, significantly increasing usage difficulty.

## 3. TENT Architecture Design: From Imperative to Declarative

To address these problems, we propose the TENT (Transfer Engine) architecture. The core idea is **evolving the transfer engine from a passive communication library to an active orchestrator**.

![](TENT's overall architecture diagram, showing four key layers: declaration interface layer, orchestration decision layer, transport abstraction layer, and physical execution layer.)

> The above diagram is excerpted from [our recently published technical report](https://arxiv.org/pdf/2604.00368)

### 3.1 Core Design Philosophy

Declarative access intent is essentially a task description method of "just state goals, don't prescribe processes." The application layer no longer needs to write complex logic to specify "how to move data," but tells the system through a structured protocol: "I need to move which data where, and complete within how long or at what priority (SLO)."

> **Difference from imperative approaches**: Although it also specifies local and remote addresses and lengths, the key distinction is:
> - TENT only provides transparent Segment semantics to the outside
> - Segment's metadata representation and Transport scheduling usage mechanisms are entirely TENT's own business

### 3.2 Declarative API Interface

```cpp
struct tent_request {
    int opcode;
    void* source;
    tent_segment_id_t target_id;  // Only specify target segment, don't care about specific transport path
    uint64_t target_offset;
    uint64_t length;
    int priority;                 // QoS priority: use TENT_PRIO_HIGH/MEDIUM/LOW
};

int tent_submit(tent_engine_t engine, tent_batch_id_t batch_id,
                tent_request_t* entries, size_t count);
```

In the `tent_submit` interface design, the first parameter formally inherits from traditional network proxy context handles (like `NIXLAgent`), but its connotation has evolved into the orchestrator's global scheduling entry point. This parameter is no longer limited to driving a single transport protocol, but serves as the TENT runtime environment proxy, supporting atomic batch submission of heterogeneous access intents. This design allows the application layer to initiate a series of tasks pointing to different physical media in a single `submit` call: for example, developers can include in a single batch request both synchronous tasks of migrating some KVCache to remote node DRAM and asynchronous tasks of persisting other logical segments to local SSD as Checkpoint tasks. Since `tent_submit` can concurrently drive underlying RDMA, NVLink, and `io_uring` backends, the system eliminates cross-protocol switching semantic overhead in a unified execution context while maintaining Segment logical consistency, maximizing heterogeneous link parallel throughput capabilities.

## 4. Four-Layer Architecture Design

TENT achieves precise transformation from logical intent to physical execution through a decoupled architecture:

![](/images/tent-internal-arch/tent-layers.png)

### 4.1 Declaration Interface Layer: Intent Description and SLO Constraints

The application layer uses declarative APIs to express Segment-centric access intents and SLO constraints, completely shielding underlying hardware parameters. Developers don't need to care about using specific protocols like RDMA, NVLink, or others—just declare transfer goals and performance requirements.

### 4.2 Orchestration Decision Layer: Late Binding and Path Synthesis

The orchestration layer acts as the system brain, using Segment Manager to retrieve multidimensional metadata (including topology weights and heterogeneous protocol credentials), dynamically solving optimal transmission plans at runtime through "late binding."

![](/images/tent-internal-arch/orchestrator-workflow.png)

### 4.3 Transport Abstraction Layer: Unified Backend Interface

The unified transport backend layer slices tasks into fine-grained pieces and executes slice spraying based on real-time telemetry data, distributing data flows in parallel across multiple physical links.

### 4.4 Physical Execution Layer: Multi-Protocol Heterogeneous Support

The physical execution layer ensures physical execution deterministically meets upper-layer business goals in complex heterogeneous environments, supporting multiple protocols like RDMA, NVLink, `io_uring`, etc.

## 5. Core Architecture Components

TENT achieves precise transformation from logical intent to physical execution through decoupled architecture: the application layer uses declarative APIs to express Segment-centric access intents and SLO constraints, completely shielding underlying hardware parameters; the orchestration layer acts as the system brain, using Segment Manager to retrieve multidimensional metadata (including topology weights and heterogeneous protocol credentials), dynamically solving optimal transmission plans at runtime through "late binding"; finally, the unified transport backend layer slices tasks into fine-grained pieces based on real-time telemetry data and executes slice spraying, distributing data flows in parallel across RDMA, NVLink, or `io_uring` physical links, ensuring physical execution deterministically meets upper-layer business goals in complex heterogeneous environments.

### 5.1 Unified Segment (Unified Segment)

We first abstract DRAM, HBM, Disk entirely as Unified Segments. For upper-layer applications (KVCache, Checkpoint), data is no longer stored in specific "video memory addresses" or "file paths," but as logical Segments. TENT internally maintains a Segment Manager to record mappings between Segments and underlying physical media.

![](/images/tent-internal-arch/segment-metadata.png)

**Core Design Philosophy:**

TENT transforms physically dispersed, heterogeneous memory resources (virtual address spaces) into system-schedulable, interoperable logical objects. Each Segment contains three core dimensions:

#### A. Buffers Dimension: Multi-Protocol Coexistence

For the same memory, TENT simultaneously maintains credentials and metadata for multiple Transports. Under one physical Buffer, it records both supported Transport lists (like NVLink, RDMA, TCP) and protocol-specific metadata for each. This design solves interoperability issues mixing NVIDIA, AMD, and domestic cards.

#### B. Topology Dimension: Intelligent Routing Foundation

Based on physical distance (like PCIe topology, NUMA affinity), hardware devices are divided into different tiers:
- **Tier-1** (fastest path): Intra-machine NVLink direct connection, highest bandwidth, lowest latency
- **Tier-2** (cross PCIe root node): Same physical node but requires crossing PCIe topology
- **Tier-3** (cross NUMA node): As fallback path, typically involves cross-NUMA access

The orchestrator calculates expected costs for different paths based on this tier list, prioritizing optimal physical paths while meeting SLO constraints.

#### C. Devices Dimension: Hardware Capability Standardization

Physical devices (like RDMA NICs) are abstracted as entities with specific types and protocol metadata. Whether underlying is Mellanox NIC or domestic accelerator chip's proprietary interconnect protocol, everything gets standardized at this layer.

> 💡 **Detailed Implementation**: For specific data structure definitions (`SegmentDesc`, `BufferDesc`, `Topology` classes, etc., please refer to "TENT Internal #2: The Orchestrator"

### 5.2 Unified Transport Backend

The transport backend is the layer in TENT architecture that directly interacts with hardware drivers and network protocol stacks. Its core goal is providing the upper orchestration layer with a multi-dimensional physical capability pool while shielding implementation differences across different transport protocols in initialization, memory registration, and data movement.

**Core Design Features:**

- **Pluggable Integration**: Different transport protocols (like NVLink, RDMA, `io_uring`) can access the system in a pluggable manner
- **Unified Operation Primitives**: Whether RDMA's `verbs` operations or file system's `read`/`write`, all get abstracted as asynchronous read/write requests targeting Segment Slices
- **Concurrent Operation**: System automatically activates all available backend modules at startup based on environment, dynamically switching or using multiple backends in parallel based on real-time link conditions

**Supported Transport Protocols:**
- **Memory-level**: NVLink (intra- and inter-node), Ascend HIXL, SHM/CXL
- **Network-level**: RDMA, TCP
- **Storage-level**: `io_uring`, GDS, Buffered I/O

![](/images/tent-internal-arch/transport-backends.png)

### 5.3 Orchestrator

The orchestrator acts as the "central brain" role in heterogeneous clusters, with the core task of transforming application-layer abstract declarative access intents into precise physical execution instructions.

**Core Capabilities:**
- **Late Binding**: Postpone path resolution to the moment of transmission request submission, rewrite execution plans based on instantaneous state
- **Autonomous Path Synthesis**: When no direct path exists, automatically synthesize multi-hop paths (e.g., GPU→DRAM→RDMA→DRAM→GPU)
- **Real-time Telemetry-Driven**: Make scheduling decisions based on real-time link state and performance prediction

![](/images/tent-internal-arch/orchestrator-workflow.png)

> 💡 **Detailed Implementation**: For orchestrator's specific algorithms, data structures, and implementation details, please refer to "TENT Internal #2: The Orchestrator"

## 6. Architecture Advantages and Production Validation

### 6.1 Three Core Advantages

Through the evolution from imperative to declarative architecture, TENT achieves three key breakthroughs:

**1. From Communication Silos to Unified Resource Pool**
- **Problem**: Existing frameworks bind transmission metadata to specific backends, treating interconnects as isolated territories
- **Solution**: TENT unifies heterogeneous interconnects into a unified resource pool through unified Segment abstraction, supporting cross-protocol dynamic scheduling

**2. From State-Blind Scheduling to Intelligent Orchestration**
- **Problem**: Static hashing or round-robin scheduling cannot sense network state, leading to bandwidth stranding
- **Solution**: TENT performs intelligent scheduling based on real-time telemetry and predictive modeling, maximizing resource utilization

**3. From Fragile Operations to Automated Self-Healing**
- **Problem**: Hardware failures require application layer intervention, cannot meet continuous availability requirements of the agent era
- **Solution**: TENT moves fault handling from application layer to data plane, achieving sub-50ms automated self-healing

### 6.2 Production Deployment Validation

TENT has been deployed in large industrial environments as common data movement infrastructure for both inference and reinforcement learning pipelines:

**Performance Improvements:**
- **LLM Inference**: Achieved 1.36× throughput improvement and 26% P90 TTFT reduction in SGLang HiCache
- **RL Pipelines**: Accelerated model weight updates by 20-26% in Moonshot Checkpoint Engine over Mooncake TE
- **Microbenchmarks**: On eight-rail 200 Gbps RDMA fabric, increased throughput by 33% and P99 latency to 27.6% of baseline established by Mooncake TE, NIXL, and UCCL-P2P

**Portability:**
- Runs unmodified across six hardware ecosystems and seven transport protocols
- Decouples ecosystem-specific logic through lightweight backends (< 800 LOC) while maintaining native performance
- Enables heterogeneous deployment without engineering compromise while maintaining throughput

**Reliability:**
- Masks both fail-stop failures and soft degradations, restoring throughput within 50ms through Telemetry-Driven Slice Spraying and Proactive Dual-Layer Resilience
- Validated through one-year, thousand-GPU GPU production deployment, transparently handles persistent interconnect churn, converting fabric instabilities into minor, transient performance fluctuations

![](/images/tent-internal-arch/performance-summary.png)

## 7. Conclusion and Outlook

The TENT architecture represents a paradigm shift from passive communication libraries to active orchestration planes. Through declarative interfaces, late binding, and intelligent orchestration, TENT achieves in heterogeneous interconnect environments:

- **Development Simplification**: Applications don't need to care about underlying transport details through declarative interfaces, simplifying development
- **Performance Optimization**: Intelligent scheduling based on real-time telemetry maximizes heterogeneous resource utilization  
- **Operations Friendly**: Automated fault recovery and path redirection reduce manual intervention

The currently released TENT orchestrator, while achieving the leap from imperative to declarative, still has hardcoded transport strategy decision logic. When facing increasingly complex application scenarios, this static design gradually shows flexibility limitations. We are subsequently redesigning the orchestrator's decision layer, introducing a configuration-file-based policy engine to further enhance system flexibility and programmability.

Stay tuned for future technical sharing!

> **Series Navigation**:
> - **TENT Internal #1**: Architecture Design Overview (this article)
> - **TENT Internal #2**: Orchestrator Core Design
> - **TENT Internal #3**: Slice Spraying and QoS Mechanisms

---

Welcome to exchange technical questions in the comments section!