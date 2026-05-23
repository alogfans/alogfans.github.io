---
date: '2026-05-09T14:00:00+08:00'
title: 'TENT Internal #2: The Orchestrator (Part 1)'
description: ""
comments: true
---

## 前言

在现代 GPU 集群架构中，大语言模型（LLM）的推理任务正在经历从“无状态”到“智能体化、多轮推理”的范式转变。这种转变的核心在于 KV 缓存（KVCache）角色的重新定义：它不再仅仅是计算过程中的临时副产品，而是演变成了一种必须在异构节点间频繁迁移的一等公民资产。随着模型规模和上下文长度的爆炸式增长，传统的点对点传输引擎（如 Mooncake TE v1、NIXL 和 UCCL）在数千规模的 GPU 集群中暴露出严重的架构缺陷，特别是在静态、硬编码的路径绑定模型方面，这种局限性直接导致了严重的带宽闲置和系统故障时的脆弱性。

为了应对这些挑战，TENT 作为一种声明式编排引擎应运而生。其核心设计哲学在于将传输意图（Transfer Intent）与物理执行（Physical Execution）彻底解耦。通过引入三阶段执行流水线，TENT 能够动态地将复杂的跨设备数据移动任务转化为最优化的底层指令 。本报告将深入解析编排器的第一阶段：动态编排（Dynamic Orchestration）。我们将这一过程定义为编排器的“认知决策”，其中统一段（Unified Segment）元数据抽象与传输后端（Transport Backend）构成了编排决策的“认知输入” 。通过对晚期绑定（Late Binding）和自主路径合成（Path Synthesis）逻辑的深度剖析，本文旨在论证 TENT 如何在异构互连网络中实现最优的路径分发。

## 1. 编排器的认知输入：元数据抽象与传输后端

动态编排的第一步并非执行传输，而是构建对集群物理拓扑与可用资源的深度认知。在 TENT 的架构中，这种认知是通过“统一段表示（Unified Segment Representation）”和“可插拔传输插件（Pluggable Transport Plugins）”共同实现的 。这两者不仅隔离了硬件的异构性，还为编排引擎提供了决策所需的感知数据。

### 1.1 统一段（Unified Segment）：位置无关的命名空间
传统的通信框架（如 UCX 或原生 RDMA）强迫应用程序直接管理后端相关的描述符，例如 RDMA 的内存密钥（rkey）或 CUDA 的 IPC 句柄。这种做法在异构集群中导致了“通信孤岛”，因为不同厂商、不同层次的互连网络拥有完全不兼容的寻址机制。TENT 通过统一段抽象解决了这一问题，将 GPU/NPU HBM、主机 DRAM 以及 NVMe-oF 等异构存储域标准化为一个全局唯一的、位置无关的命名空间。

根据 TENT 的元数据视图，每一个“段（Segment）”都包含三个核心子结构，这些数据构成了编排器的原始认知输入：

![](/images/tent-internal-arch/segment-metadata.png)

#### 1.1.1 Devices & Buffers
首先，与 TE 类似，每个段需要枚举所有可用的硬件控制器（如特定 RNIC）及其传输特定的私有数据（用 `Devices` 数组表示），并为编排器提供路径可达性（Reachability）的真值判断依据。此外，缓冲区 (`Buffers`）具体定义数据访问的合法边界及所支持的传输后端列表。它们的数据结构如下所示。

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

#### 1.1.2 Topology
TENT 所用的 `Topology` 建立了协议无关的通用拓扑模型。

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

对编排决策的影响在于，编排器可以依据上述信息，决定路径搜索的优先级，优先匹配物理距离最短的互连。我们会在切片调度算法部分进行进一步的介绍。

### 1.2 传输插件（Transport Plugins）：功能能力的矩阵化
编排器的第二个认知输入是传输后端的“能力概况（Capability Profile）”。TENT 定义了一套统一的切片执行接口（Slice-execution Interface），将 NVIDIA 的 NVLink、AMD 的 Infinity Fabric、华为的 Ascend UB 以及标准的多轨 RDMA 等复杂技术封装为轻量级的传输插件。

传输插件不仅负责具体的字节搬运，还向编排器上报其关键属性，包括是否支持 GPU-Direct、跨节点访问能力以及当前的硬件负载状态 。编排器将这些后端视为一个“能力矩阵”。

例如，当决策引擎面临一个从远程 DRAM 到本地 GPU HBM 的传输请求时，它会查询插件矩阵，确定是否可以利用 GPUDirect RDMA 直连，或者是否需要启动路径合成逻辑。这种解耦设计使得 TENT 具备了单二进制文件的跨平台移植性，引擎能根据运行时检测到的环境动态加载对应的 `.so` 插件。这也为同一套 wheel package 适用多种品类 GPU 设备成为了可能。（目前暂未开源，敬请期待）。

#### 1.2.1 `Transport` 抽象基类
TENT 通过 `Transport` 抽象基类定义了所有传输后端必须实现的统一接口。这个抽象层将上层应用逻辑与底层传输协议完全解耦，使得系统能够在运行时动态选择最优的传输路径。上层应用只需调用 submitTransferTasks 发起传输，无需关心底层使用 RDMA、NVLink 还是其他协议。传输层的选择、优化、故障处理由 TENT 引擎自动完成。

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

#### 1.2.2 支持的传输类型
目前，TENT 的统一传输后端层已实现对主流硬件协议的深度适配，将其抽象为可由编排器调度的物理能力池：

| 传输协议 | 覆盖范围 | 零拷贝 (Zero-copy) | CPU 参与度 | 适用介质 |
| --- | --- | --- | --- | --- |
| **NVLink** | 机内 / 超节点 | 原生支持 | 极低 | GPU HBM ↔ GPU HBM |
| **SHM / CXL** | 单机进程间 | 原生支持 | 中 | DRAM ↔ DRAM |
| **RDMA** | 跨节点集群 | 原生支持 | 极低 | DRAM / HBM 混合存取 |
| **AscendDirect** | 昇腾专有链路 | 原生支持 | 极低 | NPU HBM ↔ NPU HBM |
| **GDS** | 本地 / 存储网 | 原生支持 | 极低 | GPU HBM ↔ NVMe SSD |
| **IO_URING** | 本地存储 | 不支持 | 中 | DRAM ↔ NVMe / HDD |
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


## 2. 晚期绑定（Late Binding）
在传统的 Mooncake TE v1 或 NIXL 架构中，传输路径是在初始化或连接建立阶段确定的（早期绑定），这类似于在公路旅行开始前就选定了死板的固定线路，一旦途中发生拥堵或封路，系统就会陷入瘫痪。TENT 的动态编排核心在于将路径解析推迟到传输请求提交的瞬间，即所谓的“晚期绑定” 。

当应用程序通过声明式接口提交一个传输意图（如 KVCache 迁移）并获取一个不透明的批处理句柄时，编排器开始进行“无状态解析” 。由于应用层不再持有任何 stateful 的端点句柄，编排器拥有绝对的自由度来根据瞬时状态重写执行计划。

晚期绑定的逻辑流程遵循以下目标：
- **取支持的传输后端交集：** 编排器获取源和目标段的元数据，并在传输插件集合中寻找能同时满足两者寻址要求的“交集” 。
- **依据亲和性进行智能排序：** 应用 Tier-aware 策略。如果检测到两台设备位于同一 NVLink 域（Tier-1），编排器将强制锁定高性能直连路径，避免数据流向低速的 RDMA 网络，从而消除通信孤岛 。
- **QoS 约束匹配：** 根据应用定义的 SLO（如 TTFT 优先级），规则引擎会过滤掉当前排队深度过高的 RNIC，即使这些 NIC 在物理上是可达的。

这种机制的深远意义在于，它让传输引擎从一个被动的库进化为一个自治的控制平面。在生产环境中，如果某条 RDMA 轨道因为链路抖动（Link Flap）而导致吞吐量骤降，TENT 的晚期绑定逻辑会在下一次请求提交时自动避开故障轨道，而无需应用程序感知或重新初始化。

## 3. 自主路径合成（Path Synthesis）

实现在现代 GPU 超级节点（如 NVIDIA GB200 NVL72）中，物理连接的复杂性往往超出了单一协议的寻址范围 。例如，直接的 P2P 连接可能受限于 PCIe 根联合体（Root Complex）的拓扑边界。当编排器发现源和目标之间不存在任何直接路径时，它不会返回错误，而是启动“自主路径合成”逻辑 。

自主路径合成是 TENT 处理异构性最强力的武器。它将一个逻辑上的端到端传输分解为多个物理上可执行的阶段，并利用中间分段缓冲区（Staging Buffers）进行中转 。以“远程 DRAM 访问”为例，其合成路径通常包含三个子任务：
- 阶段 A (Device-to-Host)：从本地 GPU 将数据块搬运至本地 NUMA 亲和的主机 DRAM 。
- 阶段 B (Host-to-Host)：通过多轨 RDMA 将数据从中转 DRAM 发送至目标节点的主机 DRAM 。
- 阶段 C (Host-to-Device)：从目标节点的主机 DRAM 将数据搬运至最终的 GPU HBM 。

编排器的决策深度体现在对这三个阶段的并行化调度上。TENT 采用了精细的切片流水线（Pipelining），其加速逻辑可以用以下数学关系描述：

设总长度为 $M$ 的数据被划分为 $n$ 个块，则完成时间 $T_{total}$ 约等于：

$$T_{total} \approx \sum T_{startup} + \max(T_{D2H}, T_{H2H}, T_{H2D}) \times n$$

由于 TENT 能够重叠执行连续的阶段（例如，$n$ 块的 RDMA 传输与 $n-1$ 块的本地 PCIe 拷贝并发进行），它极大地掩盖了中间中转的延迟，使合成路径的表现逼近原生直连路径。

## 4. 结论
通过对 TENT 第一阶段动态编排逻辑的深度解析，我们可以得出结论：现代 LLM 服务的基础设施必须从“静态通信库”转向“自治编排平面” 。TENT 将元数据抽象和传输插件视为认知输入，通过晚期绑定解决了路径选择的僵化问题，通过自主路径合成弥合了异构互连的物理裂痕 。在生产环境的验证中，这种编排逻辑直接转化为 SGLang HiCache 吞吐量 1.36 倍的提升以及 P90 TTFT 26% 的下降 。更重要的是，它将曾经需要人工干预的硬件故障和拓扑限制，降级为数据面内部透明的重路由事件 。随着生成式 AI 进入智能体和超长上下文的时代，这种能够动态、弹性、弹性地管理集群带宽资产的编排引擎，必将成为 disaggregated AI 架构的基石 。

（未完待续）

---

这是一个系列，后面争取不断更！欢迎大家在下面的评论区交流 :)
