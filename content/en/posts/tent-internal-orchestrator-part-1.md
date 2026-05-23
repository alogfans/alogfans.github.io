---
date: '2026-05-09T14:00:00+08:00'
title: 'TENT Internal #2: Orchestrator Core Design'
description: "Deep dive into TENT orchestrator's core data structures, interface design, and implementation details"
comments: true
math: true
---

> **Series Navigation**:
> - **TENT Internal #1**: [Architecture Design Overview](/en/posts/tent-internal-arch/) - From imperative to declarative architecture evolution
> - **TENT Internal #2**: [Orchestrator Core Design](/en/posts/tent-internal-orchestrator-part-1/) - Late binding and path synthesis
> - **TENT Internal #3**: [Slice Spraying and QoS Mechanisms](/en/posts/tent-internal-slice-spraying-and-qos/) - Dynamic scheduling algorithms and performance optimization

## 1. Orchestrator Architecture Overview

The orchestrator is TENT's core component, shouldering the critical responsibility of transforming application-layer declarative transfer intents into precise physical execution instructions. Unlike traditional imperative interfaces, TENT's orchestrator achieves a fundamental transformation from configuration-driven to intent-driven through a "late binding" mechanism.

### 1.1 Core Responsibilities of the Orchestrator

The orchestrator plays the "central brain" role in the TENT architecture, with main responsibilities including:

- **Cognition Building**: Building deep understanding of cluster physical topology and available resources through unified Segment metadata abstraction and transport plugins
- **Dynamic Decision Making**: Dynamically solving optimal transmission plans based on real-time telemetry data and SLO constraints
- **Path Management**: Enabling intelligent cross-protocol path selection through late binding and path synthesis
- **Fault Recovery**: Converting hardware faults from application-layer exception handling to data-plane routine routing events

## 2. Orchestrator's Cognitive Inputs: Metadata Abstraction and Transport Backends

The first step of dynamic orchestration is not executing transfers but building deep understanding of cluster physical topology and available resources. In TENT's architecture, this cognition is achieved through "Unified Segment Representation" and "Pluggable Transport Plugins" working together. These not only isolate hardware heterogeneity but also provide the perceptual data needed for orchestration engine decision-making.

> 💡 **Architecture Overview**: For TENT's overall architecture design and core philosophy, please refer to "TENT Internal #1: Architecture Design Overview"

### 2.1 Unified Segment: Location-Independent Namespace

Traditional communication frameworks (like UCX or native RDMA) force applications to directly manage backend-related descriptors, such as RDMA memory keys (rkey) or CUDA IPC handles. This practice creates "communication silos" in heterogeneous clusters because different vendors and different interconnect layers have completely incompatible addressing mechanisms. TENT solves this problem through unified Segment abstraction, standardizing heterogeneous storage domains like GPU/NPU HBM, host DRAM, and NVMe-oF into a globally unique, location-independent namespace.

According to TENT's metadata view, each "Segment" contains three core substructures, forming the orchestrator's raw cognitive inputs:

![](/images/tent-internal-arch/segment-metadata.png)

#### 2.1.1 Devices & Buffers

First, similar to TE, each segment needs to enumerate all available hardware controllers (like specific RNICs) and their transmission-specific private data (represented by the `Devices` array), and provide the orchestrator with truth-judgment basis for path reachability. Additionally, `Buffers` specifically define the legal boundaries of data access and lists of supported transport backends. Their data structures are shown below:

```cpp
// tent/include/tent/runtime/segment.h

// Segment descriptor: Segment's top-level structure
struct SegmentDesc {
  std::string name;              // Globally unique identifier, can be hostname (with port)
  SegmentType type;              // Memory | File
  std::string machine_id;        // Owning node ID
  std::string rpc_server_addr;   // Metadata service address
  std::variant<MemorySegmentDesc, FileSegmentDesc> detail;
};

// Memory segment descriptor: Memory-type Segment's specific implementation
struct MemorySegmentDesc {
  Topology topology;                      // Topology: device interconnection relationships
  std::unordered_map<std::string, std::string> device_attrs;
  std::vector<DeviceDesc> devices;        // Devices: NIC/GPU list
  std::vector<BufferDesc> buffers;        // Buffers: memory regions
  // Transport protocol-specific attributes (key: TransportType enum, value: JSON string)
  std::unordered_map<int, std::string> transport_attrs;
};

// Device descriptor
struct DeviceDesc {
  std::string name;         // Device name, like "mlx5_0"
  std::unordered_map<TransportType, std::string> transport_attrs;
                            // Transport protocol-specific attributes (for RDMA, includes GID, etc.)
};

// A contiguous range in memory segment
struct BufferDesc {
  uint64_t addr;            // Starting offset
  uint64_t length;          // Length
  std::string location;     // Location, matching with topology info to build preferred device list
  std::vector<TransportType> transports;  // Supported Transport list
  // Transport protocol-specific attributes (for RDMA, includes rkey, etc.)
  std::unordered_map<TransportType, std::string> transport_attrs;
};
```

Compared to Mooncake TE, our improvement in this area is reflected in stripping protocol-related parts from core data structures and encapsulating them through unified `transport_attrs` fields.
- In the old design, transport protocol-specific fields (like `rkey`, `lid`, `mnnvl_handle`) were directly scattered in `BufferDesc` and `DeviceDesc`, causing core engines to perceive protocol details. The new design encapsulates these uniformly through `transport_attrs`, with core engines only querying capabilities through the `transports` list without parsing protocol-specific content.
- When adding new transport protocols, only extending the `TransportType` enum and corresponding `Transport` implementation is needed, without modifying core data structures. Core engines dynamically discover available protocols through the `transports` list, achieving true protocol independence.

#### 2.1.2 Topology

TENT's `Topology` establishes a protocol-independent general topology model:

```cpp
class Topology {
  public:
      // Affinity tier count
      const static size_t DevicePriorityRanks = 3;
      enum NicType { NIC_RDMA, NIC_TCP, NIC_UNKNOWN };
      enum MemType { MEM_HOST, MEM_CUDA, MEM_ROCM, MEM_ASCEND, MEM_UNKNOWN };
      struct NicEntry {
          std::string name;
          std::string pci_bus_id;
          NicType type;
          int numa_node;
      };
      struct MemEntry {
          std::string name;        // Like cuda:0, matches location mentioned earlier
          std::string pci_bus_id;
          MemType type;
          int numa_node;
          // Device lists by affinity tier
          std::vector<NicID> device_list[DevicePriorityRanks];
          // device_list[0]: Tier-1 (native high-speed path)
          // device_list[1]: Tier-2 (cross PCIe root node)
          // device_list[2]: Tier-3 (cross NUMA node)
      };
      // Auto-discovery: enumerate GPUs, NICs, NUMA relationships in system
      Status discover(const std::vector<Platform*>& platforms);
};
```

During initialization, TENT populates these hierarchical structures through automated topology discovery. It enumerates all available resources and divides their interconnection links into protocol-independent affinity tiers:

- **Tier-1 (native high-speed path)**: For example, intra-machine NVLink direct connection, highest bandwidth, lowest latency
- **Tier-2 (cross PCIe root node path)**: In same physical node but requires crossing PCIe topology
- **Tier-3 (cross NUMA node path)**: As fallback path, typically involves cross-NUMA access

The impact on orchestration decisions lies in that the orchestrator can decide path search priorities based on this information, prioritizing physical shortest interconnects first. We'll provide further introduction in the slice scheduling algorithm section.

### 2.2 Transport Plugins: Functional Capability Matrix

The orchestrator's second cognitive input is the "Capability Profile" of transport backends. TENT defines a unified slice execution interface, encapsulating complex technologies like NVIDIA's NVLink, AMD's Infinity Fabric, Huawei's Ascend UB, and standard multi-rail RDMA into lightweight transport plugins.

Transport plugins are not only responsible for specific byte movement but also report their key attributes to the orchestrator, including whether they support GPU-Direct, cross-node access capabilities, and current hardware load states. The orchestrator views these backends as a "capability matrix."

For example, when the decision engine faces a transmission request from remote DRAM to local GPU HBM, it queries the plugin matrix to determine if GPUDirect RDMA direct connection can be utilized, or whether path synthesis logic needs to be initiated. This decoupled design gives TENT single-binary cross-platform portability, where the engine can dynamically load corresponding `.so` plugins based on runtime-detected environments. This also enables one wheel package to suit multiple GPU device types. (Currently not open source, stay tuned.)

#### 2.2.1 `Transport` Abstract Base Class

TENT defines unified interfaces that all transport backends must implement through the `Transport` abstract base class. This abstraction layer completely decouples upper application logic from underlying transport protocols, enabling the system to dynamically select optimal transmission paths at runtime. Upper applications only need to call `submitTransferTasks` to initiate transfers without caring whether underlying uses RDMA, NVLink, or other protocols. Transport layer selection, optimization, and fault handling are automatically handled by the TENT engine.

```cpp
// tent/include/tent/runtime/transport.h

// Transport capability description
struct Capabilities {
    bool dram_to_dram;   // Host to host transmission
    bool dram_to_gpu;    // Host to GPU transmission
    bool gpu_to_dram;    // GPU to host transmission
    bool gpu_to_gpu;     // GPU to GPU transmission
    bool dram_to_file;   // Host to storage device transmission
    bool gpu_to_file;    // GPU to storage device transmission
};

class Transport {
  public:
      // Sub-batch: Minimum scheduling unit for transmission tasks
      struct SubBatch {
          virtual ~SubBatch() {}
          virtual size_t size() const = 0;
      };

      using SubBatchRef = SubBatch*;

      // === Batch management ===
      virtual Status allocateSubBatch(SubBatchRef& batch, size_t max_size);
      virtual Status freeSubBatch(SubBatchRef& batch);

      // === Transmission operations ===
      virtual Status submitTransferTasks(SubBatchRef batch,
                                         const std::vector<Request>& request_list);
      virtual Status getTransferStatus(SubBatchRef batch, int task_id,
                                       TransferStatus& status);

      // === Memory management ===
      virtual Status allocateLocalMemory(void** addr, size_t size,
                                         MemoryOptions& options);
      virtual Status freeLocalMemory(void* addr, size_t size);
      virtual bool warmupMemory(void* addr, size_t length);
      virtual Status addMemoryBuffer(BufferDesc& desc,
                                     const MemoryOptions& options);
      virtual Status addMemoryBuffer(std::vector<BufferDesc>& desc_list,
                                     const MemoryOptions& options);
      virtual Status removeMemoryBuffer(BufferDesc& desc);

      // === Notification mechanism (optional) ===
      virtual bool supportNotification() const;
      virtual Status sendNotification(SegmentID target_id,
                                      const Notification& notify);
      virtual Status receiveNotification(std::vector<Notification>& notify_list);

      // === Capability query ===
      virtual const Capabilities capabilities() const;
      virtual const char* getName() const;

  protected:
      Capabilities caps;
};
```

- **Batch Management**: `SubBatch` is the minimum scheduling unit for transmission tasks. `allocateSubBatch` creates batch containers, `freeSubBatch` releases resources. Batch design allows transport layers to merge and optimize multiple requests.

- **Transmission Operations**: `submitTransferTasks` is the core interface for initiating asynchronous data transmission. Each task in the request list describes source address, destination address, data length, and operation type. This interface returns immediately, with actual transmission happening in the background. `getTransferStatus` queries task completion status.

- **Memory Management**: Transport layers need to know which memory regions can participate in transmission. `addMemoryBuffer` registers local memory buffers; transport layers complete memory registration, permission configuration, and other operations. `allocateLocalMemory` and `freeLocalMemory` provide memory allocation services. `warmupMemory` pre-locks pages before NUMA discovery to avoid page fault interrupts affecting performance.

- **Notification Mechanism**: Notification mechanism is optional functionality for notifying the counterpart after transmission completes, currently supporting TCP and RDMA modes. `supportNotification` probes capability, `sendNotification` and `receiveNotification` deliver control messages.

- **Capability Query**: `capabilities` declares what data paths this transport layer supports; the engine judges whether it's suitable for specific requests.

#### 2.2.2 Supported Transport Types

Currently, TENT's unified transport backend layer has implemented deep adaptation for mainstream hardware protocols, abstracting them into physical capability pools schedulable by the orchestrator:

| Transport Protocol | Coverage | Zero-copy | CPU Involvement | Applicable Media |
| --- | --- | --- | --- | --- |
| **NVLink** | Intra-machine / Hyperscaler | Native support | Extremely low | GPU HBM ↔ GPU HBM |
| **SHM / CXL** | Single-machine process | Native support | Medium | DRAM ↔ DRAM |
| **RDMA** | Cross-node cluster | Native support | Extremely low | DRAM / HBM hybrid access |
| **AscendDirect** | Ascend proprietary link | Native support | Extremely low | NPU HBM ↔ NPU HBM |
| **GDS** | Local / Storage network | Native support | Extremely low | GPU HBM ↔ NVMe SSD |
| **IO_URING** | Local storage | No support | Medium | DRAM ↔ NVMe / HDD |
| **TCP** | Global universal | No support | High | Most universal |

#### Scale-out Expansion Network: Cross-Node High-Performance Foundation
- **RDMA (IB/RoCE)**: Designed for large-scale clusters. Utilizing zero-copy and kernel bypass technology, provides microsecond-level latency and extremely high aggregated bandwidth, the core path for cross-node data movement.
- **AscendDirect**: Deep optimization for Huawei Ascend (Ascend) ecosystem. Through adapting the HIXL framework, achieves efficient interconnect between NPUs, fully leveraging domestic compute platform's transmission characteristics.

#### Scale-up Enhanced Network: Intra-Machine/Hyperscaler Extreme Interconnect
- **NVLink**: Top-tier communication solution between NVIDIA GPUs. Supports both intra-machine (Intra-Node) and inter-machine (Inter-Node) sub-backends, achieving highest bandwidth data exchange between GPUs without CPU participation through GPU-Direct technology, the preferred choice for model parallelism traffic.

#### Storage and File Systems
- **GDS (GPUDirect Storage)**: Enables "high-speed direct access" between video memory and storage. By bypassing host CPU and system memory buffers, data flows directly between NVMe storage and GPU, greatly improving I/O-intensive task efficiency.
- **IO_URING**: Leverages Linux's latest high-performance asynchronous I/O primitives. Effectively reduces system call overhead, providing non-blocking, high-throughput storage access support for local storage access.

#### General Protocols
- **TCP/IP**: System's "fallback" transmission solution. Provides ultimate universality and deployment flexibility, ensuring TENT can still achieve logical connectivity even in complex network environments or cross-datacenter scenarios.
- **SHM / CXL**: Achieves nearly "zero-overhead" data exchange through shared memory or CXL protocols, the shortest path for machine-internal or hyperscaler state synchronization and rapid data sharing.

## 3. Late Binding (Late Binding)

In traditional Mooncake TE v1 or NIXL architectures, transmission paths are determined at initialization or connection establishment phase (early binding), which is like selecting rigid, fixed routes before a road trip begins—once congestion or road closure occurs en route, the system falls into paralysis. TENT dynamic orchestration's core lies in postponing path resolution to the moment of transmission request submission, so-called "late binding."

When applications submit a transfer intent (like KVCache migration) through declarative interfaces and obtain an opaque batch handle, the orchestrator begins "stateless resolution." Since the application layer no longer holds any stateful endpoint handles, the orchestrator has absolute freedom to rewrite execution plans based on instantaneous state.

Late binding's logical flow follows these objectives:
- **Take intersection of supported transport backends**: Orchestrator obtains source and target segment metadata, then searches within transport plugin sets for "intersection" that can simultaneously satisfy both addressing requirements.
- **Smart sorting based on affinity**: Apply Tier-aware strategy. If two devices are detected in the same NVLink domain (Tier-1), the orchestrator forcibly locks the high-performance direct path, avoiding data flowing to slower RDMA networks, thereby eliminating communication silos.
- **QoS constraint matching**: Based on application-defined SLOs (like TTFT priority), rule engines filter out RNICs with excessive current queue depths, even if these NICs are physically reachable.

This mechanism's profound significance lies in evolving the transfer engine from a passive library to an autonomous control plane. In production environments, if an RDMA rail's throughput drastically degrades due to link flap (Link Flap), TENT's late binding logic will automatically avoid that failed rail in the next request submission without application perception or re-initialization.

## 4. Autonomous Path Synthesis (Path Synthesis)

In modern GPU hyperscalers like NVIDIA GB200 NVL72, physical connection complexity often exceeds single-protocol addressing scope. For example, direct P2P connections might be limited by PCIe root complex topology boundaries. When the orchestrator discovers no direct path exists between source and destination, it won't return an error but initiates "autonomous path synthesis" logic.

Autonomous path synthesis is TENT's most powerful weapon for handling heterogeneity. It decomposes one logical end-to-end transmission into multiple physically executable stages, utilizing intermediate staging buffers for transit. Taking "remote DRAM access" as an example, its synthesis path typically contains three subtasks:

- **Stage A (Device-to-Host)**: Move data blocks from local GPU to local NUMA-affine host DRAM
- **Stage B (Host-to-Host)**: Send data from staging DRAM to target node's host DRAM via multi-rail RDMA
- **Stage C (Host-to-Device)**: Move data from target node's host DRAM to final GPU HBM

The orchestrator's decision depth is embodied in parallelized scheduling of these three stages. TENT employs fine-grained slice pipelining, whose acceleration logic can be described by the following mathematical relationship:

Let data of total length $M$ be divided into $n$ blocks, then completion time $T_{total}$ is approximately:

$$T_{total} \approx \sum T_{startup} + \max(T_{D2H}, T_{H2H}, T_{H2D}) \times n$$

Since TENT can overlap consecutive stages (for example, RDMA transmission of $n$ blocks concurrently with local PCIe copy of $n-1$ blocks), it greatly masks intermediate transit latency, making synthesized path performance approach native direct path performance.

## 5. Conclusion

Through deep analysis of TENT's first-stage dynamic orchestration logic, we can conclude that modern LLM service infrastructure must transition from "static communication libraries" to "autonomous orchestration planes." TENT treats metadata abstraction and transport plugins as cognitive inputs, solving the path selection rigidity problem through late binding, and bridging heterogeneous interconnect physical gaps through autonomous path synthesis. In production environment validation, this orchestration logic directly translates to 1.36× higher throughput and 26% lower P90 TTFT in SGLang HiCache. More importantly, it demotes hardware failures and topology limitations that once required manual intervention to data-plane internal transparent re-routing events. As generative AI enters the agent and long-context era, this orchestration engine capable of dynamically, elastically managing cluster bandwidth assets will become the cornerstone of disaggregated AI architectures.

(To be continued)

---

This is a series, with more coming! Feel free to exchange technical questions in the comments section below!