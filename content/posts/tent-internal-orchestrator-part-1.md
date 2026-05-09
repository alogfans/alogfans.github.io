---
date: '2026-05-09T15:00:00+08:00'
title: 'TENT Internal #2: The Orchestrator (Part 1)'
description: ""
comments: true
---

## 前言

在上一篇文章中，我们介绍了 TENT（Mooncake Transfer Engine）的整体架构。作为一款已经用到多种奇奇怪怪场合的高性能传输引擎，Mooncake 需要运行在极其多样化的硬件环境之中。除了标准的 InfiniBand/RoCE 网络外，还用到 NVLink、AMD Infinity Fabric、Ascend UB高速网络；所用的存储介质除了 DRAM 之外，还融合了NVIDIA GPU、华为昇腾 NPU、摩尔线程GPU等异构加速器的显存。

面对如此复杂的部署场景，单一传输协议显然无法满足所有需求。本文将深入探讨TENT如何通过多传输层架构和智能编排机制，在保证性能的同时实现硬件无关的统一抽象。

## 一、为什么需要多传输层？

### 1. Mooncake TE 支持的传输协议

在分布式系统中，数据传输的选择本质上是性能、成本与兼容性的权衡。没有任何一种协议能覆盖从“机内超高速互联”到“跨中心广域网”的全场景。如果只支持一种协议，引擎要么在高性能场景中捉襟见肘，要么在通用场景中难以部署。

#### RDMA (IB/RoCE/eRDMA 等)
- 技术特性：支持零拷贝与内核旁路，具备微秒级超低延迟与极高带宽。
- 核心优势：结合 peermem 机制，RDMA 能够实现 DRAM 与 GPU HBM 之间任意组合的数据传输，具有最广泛的适用性。
- 实践现状：在 Mooncake TE 的部署实践中，绝大多数用户首选单一的 RDMA 协议作为核心传输支柱。

#### 专用高速互联协议 (NVLink 等)
- 技术特性：提供 GPU 间最高的点对点带宽，峰值性能通常远超 RDMA。
- 局限性：应用场景受限，仅支持单机内或特定的超节点（Supernode）环境，且要求远端目标必须是 GPU HBM。
- 应用边界：虽然性能极强，但无法处理涉及非 GPU 介质或跨节点的通用传输需求。

#### TCP/IP
- 技术特性：具备极致的通用性与兼容性，几乎能在任何网络环境下运行，部署成本低且技术极其成熟。
- 定位演变：在第一代 Mooncake TE 中，TCP 仅作为“保底方案（Last-resort）”。但随着应用场景扩展到跨数据中心等复杂环境，其不可替代的连通性优势开始显现。

### 2. 单传输层架构的缺陷
尽管 Mooncake TE 支持上述的所有传输协议，甚至支持多个品牌的 GPU 卡。然而大家已经注意到了，TE 要求用户在初始化一个实例时就确定使用的传输协议，并在全程使用这一协议。从底层架构来看，它要求一个集群使用完全一致的传输协议，并在初始化阶段就将传输元数据（比如 RDMA 的 `rkey` 或者 NVLink 的 IPC 句柄）与特定的后端强行绑定。本质上来说，这使得同机异构的互连链路被看作是一个个彼此孤立的“领地”，而不是一个可以灵活调配的“资源池”。

#### 1. 元数据表示的缺陷
很多现有的引擎号称提供了统一的 API，但剥开外壳你会发现，它们本质上只是层“薄封装”（Thin wrappers），将接口做成一个样子。

实质上，后端特定的描述符随传输协议的不同而不同：
- Scale-out 网络：要处理 RDMA 的内存键（`ibv_reg_mr` 等）；
- Scale-up 网络：要管理 NVLink 的 IPC 句柄（`cudaIpcMemHandle` 等）。

这些描述符在不同平台边界之间互不通约。此外，即使许多国产卡提供了类似CUDA的接口层，它们本质上也不能与 NVLink 暴露的 IPC 句柄互操作，通常会导致崩溃。在之前，我们只能强迫用户使用 TCP 协议或者自己定制一套 transport。

即使是 UCX 这样的大厂作品，其集成也往往存在生态锁定（比如 NVIDIA 专用），在面对真正的异构集群（NVIDIA + AMD + 昇腾 + 摩尔线程）时，缺乏中立性。
这种语义上的不兼容，逼得运维人员不得不针对不同的硬件资源管理多个独立的引擎实例。TENT 的出现，正是为了终结这种混乱。 我们引入了统一元数据抽象，利用“晚期绑定”（Late-binding）技术，将应用逻辑与硬件驱动彻底解耦，让无缝的混合部署成为可能。

#### 2. 链路覆盖的局限性
第二个严峻挑战在于：高性能链路（Fabrics）在功能设计上往往具有极强的“排他性”，它们通常被锁死在特定的数据移动模式中。以 Scale-up 链路（如 NVLink） 为例，它天生就是为了低延迟的 GPU 到 GPU 内存拷贝而优化的。像 UCX 这样的传统引擎，主要依赖静态启发式规则（Static Heuristics）。这就好比一条只准跑跑车的专用赛道，一旦你的传输意图（Intent）稍微偏离了预设的“刚性规范”，系统就会因为无法匹配规则而彻底罢工。

我们在实际生产环境中观察到了一个非常典型的案例：

> 在一个仅配备了 Scale-up 高速链路的集群中，系统尝试执行一种 Mooncake Store 中常见的访问模式——从远程 DRAM 拉取 KVCache。由于这种模式在物理层并不受 NVLink 原生支持，且无法匹配引擎内部硬编码的“直连标准”，要么必须占用有限的 GPU 空间存放 KVCache，要么必须被迫回退到基于 TCP 的 RPC 传输。

面对这种链路层面的“物理断路”，自主编排（Autonomous Orchestration） 就成了破局的关键。TENT 的编排器不再机械地套用硬编码规则，而是具备了“因地制宜”的灵活智力；当源端和目标端无法直接互通时，编排器会自动在中间路径上分配中转缓冲区（Staging Buffers）。

下面我们对 Mooncake TENT 的编排设计进行一下精讲。

## 二、元数据表示

为了打破由碎片化存储和互联链路造成的“通信孤岛”，TENT 引入了统一段（Unified Segment）抽象。简单来说，Segment 将 GPU/NPU HBM、主机 DRAM 以及 NVMe-oF 等异构内存域归一化为一个位置无关的命名空间。通过将那些互不通约的设备描述符（如 RDMA 的 rkey 或 CUDA 的 IPC 句柄）调和到统一的元数据上下文中，Segment 实现了应用层“逻辑意图”与其“物理实现”的彻底解耦。
我们在上一篇文章进行了简要的介绍，这里结合代码再深入梳理一下。

### 1. Segment

Segment 抽象被具体化为一个层级化的元数据视图。每个 Segment 由全局唯一的逻辑名称标识，并封装了三个核心子结构：拓扑（Topology）、设备（Devices）和缓冲区（Buffers）。我们可以回顾一下元数据表示图。

![](/images/tent-internal-arch/segment-metadata.png)

```cpp
// tent/include/tent/runtime/segment.h

// 段描述符：Segment 的顶层结构 
struct SegmentDesc {
  std::string name;              // 全局唯一标识符，可以是主机名（带端口号）      
  SegmentType type;              // Memory | File
  std::string machine_id;        // 所属节点 ID
  std::string rpc_server_addr;   // 元数据服务地址
  std::variant<MemorySegmentDesc, FileSegmentDesc> detail;
};

// 内存段描述符：Memory 类型 Segment 的具体实现
struct MemorySegmentDesc {
  Topology topology;                      // 拓扑：设备互联关系
  std::unordered_map<std::string, std::string> device_attrs;
  std::vector<DeviceDesc> devices;        // 设备：NIC/GPU 列表
  std::vector<BufferDesc> buffers;        // 缓冲区：内存区域
  // 传输协议特定属性（key: TransportType enum, value: JSON string）
  std::unordered_map<int, std::string> transport_attrs;
};

// 设备描述符
struct DeviceDesc {
  std::string name;         // 设备名，如"mlx5_0"
  std::unordered_map<TransportType, std::string> transport_attrs;
                            // 传输协议特定属性（如对于 RDMA 而言，包括 GID）  
};

// 内存段中一个连续的区间
struct BufferDesc {
  uint64_t addr;            // 起始偏移量
  uint64_t length;          // 长度
  std::string location;     // 位置，与拓扑信息匹配构建优选的设备列表
  std::vector<TransportType> transports;  // 支持的 Transport 列表
  // 传输协议特定属性（如对于 RDMA 而言，包括 rkey）                              
  std::unordered_map<TransportType, std::string> transport_attrs;
};
```

和 Mooncake TE 相比，我们在这一部分的改进点体现在将传输协议相关的部分从核心数据结构中剥离，通过统一的 `transport_attrs` 字段进行封装。
- 在旧设计中，传输协议特定字段（如 `rkey`、`lid`、`mnnvl_handle`）直接散落在 `BufferDesc` 和 `DeviceDesc` 中，导致核心引擎需要感知各协议细节。新设计通过 `transport_attrs` 统一封装，核心引擎只通过 `transports` 列表查询能力，无需解析协议特定内容。
- 新增传输协议时，只需扩展 `TransportType` 枚举和对应的 `Transport` 实现，无需修改核心数据结构。核心引擎通过 `transports` 列表动态发现可用协议，实现真正的协议无关性。

### 2. Topology

`Topology` 第一代的核心特点是拓扑感知，但设计上显著以 RDMA 为中心。TENT 进行了重构，建立了协议无关的通用拓扑模型。

```cpp
class Topology {
  public:         
      // 亲和性层级数量
      const static size_t DevicePriorityRanks = 3;
      enum NicType { NIC_RDMA, NIC_TCP, NIC_UNKNOWN };
      enum MemType { MEM_HOST, MEM_CUDA, MEM_ROCM, MEM_ASCEND, MEM_UNKNOWN };
      struct NicEntry {
          std::string name;
          std::string pci_bus_id;
          NicType type;
          int numa_node;           // NUMA 节点编号
      };
      struct MemEntry {
          std::string name;        // 如 cuda:0，与前面 location 匹配
          std::string pci_bus_id;
          MemType type;
          int numa_node; 
          // 按 affinity tier 分层的设备列表
          std::vector<NicID> device_list[DevicePriorityRanks];
          // device_list[0]: Tier-1 (原生高速路径)
          // device_list[1]: Tier-2 (跨 PCIe 根节点) 
          // device_list[2]: Tier-3 (跨 NUMA 节点) 
      };
      // 自动发现：枚举系统中的 GPU、NIC、NUMA 关系
      Status discover(const std::vector<Platform*>& platforms);
  }; 
```

在初始化阶段，TENT 会通过自动化拓扑发现来填充这些层级结构。它会枚举所有可用资源，并将它们的互联链路划分为与协议无关的亲和性层级（Affinity Tiers）：
- Tier-1（原生高速路径）：例如机内 NVLink 直连，带宽最高，延迟最低。
- Tier-2（跨 PCIe 根节点路径）：在同一物理节点内但需跨越 PCIe 拓扑的连接。
- Tier-3（跨 NUMA 节点路径）：作为保底的 fallback 路径，通常涉及跨 NUMA 访问。

在执行 Slice 传输期间，TENT 首先需依据拓扑信息与动态遥测信息选择一个本地传输设备，可概括为以下步骤：
1. 查找内存条目 → `Topology::getMemEntry()` 获取对应的 `MemEntry`
2. 遍历亲和性层级 → 查找可用 NIC，并按 Tier-1 → Tier-2 → Tier-3 顺序分类
3. 设备可用性检查 → 结合 `RailMonitor` 排除故障/冷却中的设备
4. 利用 Slice Spraying 算法在上述集合中综合确定使用的本地传输设备

## 三、传输后端
### 1. Transport 基类

TENT 通过 Transport 抽象基类定义了所有传输后端必须实现的统一接口。这个抽象层将上层应用逻辑与底层传输协议完全解耦，使得系统能够在运行时动态选择最优的传输路径。上层应用只需调用 submitTransferTasks 发起传输，无需关心底层使用 RDMA、NVLink 还是其他协议。传输层的选择、优化、故障处理由 TENT 引擎自动完成。

```cpp
  // tent/include/tent/runtime/transport.h                                                            
                                                                                                      
  // 传输能力描述                                                                                     
  struct Capabilities {                                                                               
      bool dram_to_dram;   // 主机到主机传输                                                          
      bool dram_to_gpu;    // 主机到 GPU 传输                                                         
      bool gpu_to_dram;    // GPU 到主机传输                                                          
      bool gpu_to_gpu;     // GPU 到 GPU 传输 
      bool dram_to_file;   // 主机到存储设备传输
      bool gpu_to_file;    // GPU 到存储设备传输                                                      
  };                                                                                                  
                                                                                                      
  class Transport {                                                                                   
  public:                                                                                             
      // 子批次：传输任务的最小调度单元                                                               
      struct SubBatch {                                                                               
          virtual ~SubBatch() {}                                                                      
          virtual size_t size() const = 0;                                                            
      };                                                                                              
                                                                                                      
      using SubBatchRef = SubBatch*;                                                                  
                                                                                                      
      // === 批次管理 ===                                                                             
      virtual Status allocateSubBatch(SubBatchRef& batch, size_t max_size);
      virtual Status freeSubBatch(SubBatchRef& batch);                                                
                                                                                                      
      // === 传输操作 ===                                                                             
      virtual Status submitTransferTasks(SubBatchRef batch,                                           
                                         const std::vector<Request>& request_list);                   
      virtual Status getTransferStatus(SubBatchRef batch, int task_id,                                
                                       TransferStatus& status);                                       
                                                                                                      
      // === 内存管理 ===                                                                             
      virtual Status allocateLocalMemory(void** addr, size_t size,                                    
                                         MemoryOptions& options);                                     
      virtual Status freeLocalMemory(void* addr, size_t size);                                        
      virtual bool warmupMemory(void* addr, size_t length);                                           
      virtual Status addMemoryBuffer(BufferDesc& desc,                                                
                                     const MemoryOptions& options);                                   
      virtual Status addMemoryBuffer(std::vector<BufferDesc>& desc_list,                              
                                     const MemoryOptions& options);                                   
      virtual Status removeMemoryBuffer(BufferDesc& desc);                                            
                                                                                                      
      // === 通知机制（可选） ===                                                                     
      virtual bool supportNotification() const;                                                       
      virtual Status sendNotification(SegmentID target_id,                                            
                                      const Notification& notify);                                    
      virtual Status receiveNotification(std::vector<Notification>& notify_list);                     
                                                                                                      
      // === 能力查询 ===                                                                             
      virtual const Capabilities capabilities() const;                                                
      virtual const char* getName() const;                                                            
                  
  protected:
      Capabilities caps;
  };

      virtual Status receiveNotification(std::vector<Notification>& notify_list);

      // === 能力查询 ===
      virtual const Capabilities capabilities() const;
      virtual const char* getName() const;

  protected:
      Capabilities caps;
  };
```

- **批次管理** ：`SubBatch` 是传输任务的最小调度单元。`allocateSubBatch` 创建批次容器，`freeSubBatch` 释放资源。批量设计允许传输层对多个请求进行合并优化。

- **传输操作** ：`submitTransferTasks` 是核心接口，发起异步数据传输。请求列表中的每个任务描述源地址、目标地址、数据长度和操作类型。该接口立即返回，实际传输在后台进行。`getTransferStatus` 查询任务完成状态。

- **内存管理** ：传输层需要知道哪些内存区域可参与传输。`addMemoryBuffer` 注册本地内存缓冲区，传输层完成内存注册、权限配置等操作。`allocateLocalMemory` 和 `freeLocalMemory` 提供内存分配服务。`warmupMemory` 在 NUMA 探测前预锁定页面，避免缺页中断影响性能。

- **通知机制** ：通知机制是可选功能，用于在传输完毕后通知对手方，目前支持 TCP 和 RDMA 两种模式。`supportNotification` 探测能力，`sendNotification` 和 `receiveNotification` 传递控制消息。

- **能力查询** ：`capabilities` 声明该传输层支持的数据路径，引擎据此判断是否适用于特定请求。

### 2. 支持的传输类型
目前，TENT 的统一传输后端层已实现对主流硬件协议的深度适配，将其抽象为可由编排器调度的物理能力池：

| 传输协议 | 覆盖范围 | 零拷贝 (Zero-copy) | CPU 参与度 | 适用介质 |
| --- | --- | --- | --- | --- |
| **NVLink** | 机内 / 超节点 | 原生支持 | 极低 | GPU HBM $\leftrightarrow$ GPU HBM |
| **SHM / CXL** | 单机进程间 | 原生支持 | 中 | DRAM $\leftrightarrow$ DRAM |
| **RDMA** | 跨节点集群 | 原生支持 | 极低 | DRAM / HBM 混合存取 |
| **AscendDirect** | 昇腾专有链路 | 原生支持 | 极低 | NPU HBM $\leftrightarrow$ NPU HBM |
| **GDS** | 本地 / 存储网 | 原生支持 | 极低 | GPU HBM $\leftrightarrow$ NVMe SSD |
| **IO_URING** | 本地存储 | 不支持 | 中 | DRAM $\leftrightarrow$ NVMe / HDD |
| **TCP** | 全局通用 | 不支持 | 高 | 最通用 |


#### Scale-out 扩展网络：跨节点高性能基石
- RDMA (IB/RoCE)：专为大规模集群设计。利用零拷贝（Zero-copy）与内核旁路（Kernel Bypass）技术，提供微秒级延迟与极高的聚合带宽，是跨节点数据平移的核心路径。
- AscendDirect：针对华为昇腾（Ascend）生态深度优化。通过适配 HIXL 框架，实现 NPU 间的高效互连，充分发挥国产算力平台的传输特性。
#### Scale-up 增强网络：机内/超节点极致互连
- NVLink：NVIDIA GPU 间的顶级通信方案。支持机内（Intra-Node）与跨机（Inter-Node）两种子后端，通过 GPU-Direct 技术实现无需 CPU 参与的最高带宽数据交换，是模型并行流量的首选。
#### 存储与文件系统
- GDS (GPUDirect Storage)：实现显存与存储间的“高速直达”。通过绕过主机 CPU 与系统内存缓冲区，数据直接在 NVMe 存储与 GPU 之间流转，极大提升了 I/O 密集型任务的效率。
- IO_URING：利用 Linux 最新的高性能异步 I/O 原语。有效降低系统调用开销，为本地存储访问提供非阻塞、高吞吐的存取支撑。
#### 通用协议
- TCP/IP：系统的“保底”传输方案。具备极致的通用性与部署灵活性，确保 TENT 在复杂网络环境或跨数据中心场景下依然能实现逻辑连通。
- SHM / CXL：通过共享内存（Shared Memory）或 CXL 协议实现近乎“零开销”的数据交换，是机内或超节点状态同步与快速数据共享的最短路径。



（未完待续）

---

这是一个系列，后面争取不断更！欢迎大家在下面的评论区交流 :)
