---
date: '2026-04-24T15:00:00+08:00'
title: 'TENT Internal #1: The Architecture'
description: ""
comments: true
---

## 引子

我在多种场合写过很多汇报材料，回顾了 AI Stor 的发展历程：
- 2024 年是 P/D（Prefill/Decode）分离的元年，通过在物理或逻辑层面解耦算力密集型的预填充阶段与访存密集型的生成阶段，我们初步解决了推理吞吐量与时延之间的固有矛盾。**这一年 Mooncake 从一篇 arXiv 论文成长为一个可以跑的开源项目。**
- 2025 年是 KVCache 大规模应用的一年，业界通过存储解耦架构确立了“以存储换计算”的范式，KVCache 不再是即用即弃的中间变量，而是升级为驱动推理服务的核心状态资产。**这一年 Mooncake 从 vLLM 的简单适配，走向了多推理框架、多型号 GPU、多应用场景的覆盖。**
- 2026 年则是智能体（Agent）爆发的一年，相信大家在这四个月里看到了，大模型正从单纯的语义生成引擎进化为具备长程规划能力的“推理中枢”，驱动着更长序列、更高频率互动的任务闭环。

那么，作为一个 Storage for AI 的研究者，我是怎么看待这个问题的呢？

### 状态数据：不止 KVCache
KVCache 已由暂态数据演化为跨阶段复用的核心存储资产。比方说，现在大模型推理的标配“上下文记忆”很大程度要归功于池化的 KVCache 存储。另外，KVCache 在多轮对话和 AI 辅助编程等场景中有很高的缓存命中率。

> 以 Kimi (Moonshot AI) 为例，其生产环境中的 KVCache 典型命中率已高达 90%。这种极致的复用效率直接将用户的实际计费成本压低至市场标准价的 25%，在商业层面极具竞争力。

然而，2026 年智能体范式下的“状态资产”正呈现出更复杂的形态：
- **在线强化学习（Online RL）流水线中的模型权重更新。**根据 Kimi Team 的技术报告，在线 RL 需要在模型采样（Rollout）的过程中，以微秒级的精度同步数千亿参数的梯度或权重，以避免采样任务因等待模型刷新而停顿 。在 Moonshot Checkpoint Engine 的典型场景下，这些频繁的参数下发同样表现为极高密度的“全量同步大象流（Elephant Flows）” 。
- **MoE（专家混合模型）中的专家并行（Expert Parallelism, EP）。**在 MoE 架构中，Token 需要在不同的专家（Expert）之间频繁穿梭。这种专家并行流量（Mice flows）通常只有几十 KB，但它们正处于推理的执行关键路径上。

### 链路：不止 RDMA
从链路的角度上来说，多种物理特性迥异的存储介质（如DRAM、GPU/NPU显存、NVMe SSD等）及多种高速互联技术（如 NVLink、RDMA、CXL等）共同构成了复杂的非均质存储互联系统（Heterogeneous Storage Interconnect）。

![](images/tent-internal-arch/ai-cluster-topology.png)

这里我们拿一个典型的超节点拓扑举例说明。每个推理服务器上有 8 张 GPU ，它们之间通过 NVLink 或者 Ascend UB 等 Scale-up 高速互联协议连接，带宽通常达到数百 GB/s 的水平。对于GB200等超节点来讲，NVLink 可以跨越推理服务器，在一个 rack 上实现所有 GPU 的互联。有时候 CXL 等也可以当做 Scale-up 网络使用。所以，这本质是机内或者 rack 内的网络。

为了实现跨推理服务器的高效数据平面，每台服务器还安装了几块RDMA网卡，并通过两层交换机形成一个完整的Scale-out RDMA网络。为了提高聚合带宽，一般会安装4-8张卡，实现最高达到 8x400Gbps 的聚合带宽。一般情况下，这个网络在一个物理意义上的机房里。

有意思的是，有时边界甚至会跨越机房。比方说最近 Kimi 和我们团队一起提出的 PrfaaS（媒体称之为“算力预制菜”）技术，它的主要思想是让一部分 Prefill 任务和 Decode 任务在两个数据中心下运行，两个数据中心之间最多只有以太网专线。

![](images/tent-internal-arch/prfaas.png)

> 参考：https://arxiv.org/pdf/2604.15039

## Mooncake TE 看起来挺好？
这里我相信大家通过各种方式了解了 Mooncake TE 的架构。其实它的架构十分简单，这源于早期设计哲学的一连串反应。

最初，Mooncake 的核心设计深受我博士期间研究工作 SMART 的影响。实际上，当时我只做了一件事，就是从SMART copy & paste 代码，将其从单网卡改成了多网卡使用（包括那个经常出毛病的 Handshake 模块）。那时都是古法编程。显然，那是一个纯粹以 RDMA 为中心的世界：我们假设物理拓扑是相对纯粹的，通信逻辑也是命令式的，只要管好RDMA资源，性能就非常能打。尽管有 TCP，但那个纯粹是应付没有RDMA的平台下能跑通的需求。

然而，后面发现，现实环境的演进比实验室预想的要复杂得多：
1. NVLink 的强势切入：它引入了完全不同于 RDMA 的内存空间管理和 IPC handle 机制。在我们遇到的一些先进的超节点服务器上，两个机子之间甚至没有 RDMA 连接，只有 NVLink 连接！
2. 国产平台的百花齐放：从首个国产平台昇腾（以及它的 HIXL 框架）到后面加入更多的国产品牌，每家都有自己的传输协议和 API。
3. 奇奇怪怪的应用场景：一开始我们只是做 1p1d，到后来 KVCache 池化、检查点、MoE EP 等等都用上了，它们有不同的使用模式。

于是：
- 我们发现 Transport 目录越来越臃肿了。 重复的代码越来越多，每一个新平台的加入，都意味着我们要去手动适配一套新的“黑话”，代码里充斥着平台相关的条件编译分支。原本追求简洁的 Mooncake TE 逐渐变成了由各种 Backend 强行缝合起来的“怪物”，维护成本逐渐推高，且在面对复杂故障时显得极度脆弱。
- 另一方面，互操作性越来越差。现有的框架将传输元数据与特定的后端紧紧绑定。这种“端点中心化”的架构把互连看作孤立的领地，而不是一个可以灵活调配的资源池。比方说，你把 NVIDIA GPU 和国产芯片放到一个传输集群里，或者利用类似 GB200 NVL72 的设备做远程 DRAM 的数据存取（其实是无法直接支持的），就可能导致崩溃。
- 此外，这个东西也给运维人员带来了“折磨”（或者说，让他们获得了向 +1 汇报成果的机会）。在大规模集群中，硬件故障是常态，从网卡链路闪断到坏卡（我们也是后来才发现坏卡的几率很高），在要求连续可用性的智能体时代，简直是灾难。特别是 Mooncake TE 某种程度上容易扩散故障到整个集群。

其实，这一系列问题本质上是设计范式的问题。我们称之为命令式接口。简单的说，它就是底层链路的直接 API 转译（哪怕通过一些封装，让不同链路的 API 看起来一样）。

以 NIXL 为例：
```cpp
// ❌ NIXL 风格：开发者仍需介入资源与后端的绑定
// 1. 创建代理和特定的后端插件（例如显式指定 UCX）
NIXLAgent* agent = nixlCreateAgent("node_0");
NIXLBackend* ucx_be = nixlCreateBackend(agent, "UCX"); // 显式绑定特定传输技术

// 2. 注册段（Segment），注意：NIXL 的段是绑定到特定后端的
NIXLSegment* seg = nixlCreateSegment(agent, gpu_ptr, size, ucx_be);

// 3. 获取并交换元数据（Metadata）
// 开发者往往需要自己负责将这个 MD 传给远程节点，或者通过 NIXL 复杂的缓存机制管理
NIXLMetadata md = nixlGetSegmentMetadata(seg);

// 4. 创建并发布请求
NIXLXferReq* req = nixlCreateXferReq(agent, src_list, dst_list);
nixlPostXferReq(req); // 一旦选定的后端（如 UCX）在运行时出现拥塞，应用层很难介入动态切路
```
> （NIXL 逃课了，它不用自己做 Handshake！天知道 Mooncake 在 Handshake 里栽了多少坑）

很显然，在上面的 API 中，agent、backend、segment、metadata 等要素是紧密的绑定关系，它们都依赖 UCX 作为统一的传输后端并实施行动。那么如果我想从 SSD 读些数据，那么就需要分配另外一组对象，二者绝对不能混用。所以，NIXL 本身必须要绑定到具体的传输后端。

有人会问，UCX 不是可以混合使用 NVLink 和 RDMA 吗？的确，UCX 的“统一”主要体现在 UCP (Unified Communication Protocol) 层提供的 API 抽象，但在底层的 UCT (Unified Communication Transport) 实现上，厂商间的壁垒依然稳固。
- **编译时。**UCX 在编译时就决定激活 CUDA 支持还是 ROCm 支持。由于 NVIDIA 的 `libcuda.so` 和 AMD 的 `libhip_hcc.so`（或相关运行时库）在符号命名、内存映射逻辑等完全独立，生产环境中的 `libucp.so` 往往是针对单一生态构建的。对于国产加速卡（如华为昇腾、寒武纪、摩尔线程等），用户通常需要使用厂商自己维护的 UCX 私有分支，进一步导致了严重的版本碎片化。
- **运行时。**NVIDIA 使用 cudaIpcMemHandle_t 进行跨进程内存共享，而 AMD 使用其特有的 hipIpcMemHandle_t。这些句柄本质上是厂商驱动封装的私有元数据。UCX 只是将这些元数据原样提供给用户，并不具备“翻译”功能。最坏情况下，这会导致 CUDA 运行时向对端的 AMD 运行时发起请求时，导致直接降级到 TCP 甚至崩溃的后果。为了避免此问题，我们不得不分配多组 Agent 已满足不同目标的访问需求，大幅推高了使用难度。

## 转向声明式
针对这个问题，受到 Alluxio 等工作的启发，我们认为将传输引擎从一个被动的通信库（Library）升级为主动的编排器（Orchestrator），是解决非均质计算环境下通信壁垒的逻辑必然。要实现它，我们首先需要提出新的声明式 API 范式，这就是我们在 TENT 做的第一个改进（牵一发而动全身，所以我们重写了一遍实现）。

所谓声明式存取意图，本质上是一种“只提目标，不规定过程”的任务描述方式。应用层不再需要编写复杂的逻辑来指定“如何移动数据”，而是通过一个结构化的协议告诉系统：“我需要将哪些数据（如 KVCache）移动到哪里，以及必须在多长时间或多高优先级（SLO）完成。”

> 这与命令式的差异在哪里呢？看起来也是指定本地和远端地址和长度啊？
> - TENT 对外只提供透明的 Segment 语义；
> - 而 Segment 的元数据表示以及 Transport 的调度使用机理完全是 TENT 自己的事！

Mooncake TE 需要一坨参数或者环境变量去指定使用的是什么 Transport，也提供了显式的 installTransport 接口。但是在 TENT，这些东西统统去掉了！TENT 在启动的时候，会加载一组（注意不是一个）Transports。对于每个传输任务，TENT 的编排器都能自主决策使用的 Transport 完成相应的传输任务。类似于 UCX，比方说可以在条件许可的情况混合使用 NVLink 和 RDMA。当然我们超出 UCX 的范畴，也可以用相同的 API（即相同的 Transfer Engine 实例），借助  GPUDirect Storage Transport 让显卡去直接操作本地及远端的 SSD，等等。

比方说，TENT 提供了如下的 API 接口：

```cpp
struct tent_request {
    int opcode;
    void* source;
    tent_segment_id_t target_id;  // 只需指定目标segment，不关心具体传输路径
    uint64_t target_offset;
    uint64_t length;
    int priority;                 // QoS priority: use TENT_PRIO_HIGH/MEDIUM/LOW
};

int tent_submit(tent_engine_t engine, tent_batch_id_t batch_id,
                tent_request_t* entries, size_t count);
```

在 `tent_submit` 接口设计中，首位参数虽在形式上承袭了类似于传统网络代理（如 `NIXLAgent`）的上下文句柄，但其内涵已演进为编排器的全局调度入口。该参数不再仅限于驱动单一的传输协议，而是作为 TENT 运行时环境的代理，支持异构存取意图的原子化批量提交。

这种设计使得应用层可以在单次 `submit` 调用中发起一系列指向差异化物理介质的任务：例如，开发者能够在一个批处理请求中，同时包含将一部分 KVCache 迁移至远端节点 DRAM 的同步任务，以及将另一部分逻辑 Segment 异步持久化至本地 SSD 的 Checkpoint 任务。由于 `tent_submit` 能够并发驱动底层的 RDMA、NVLink 与 io_uring 后端，系统得以在统一的执行上下文中消除跨协议切换的语义开销，从而在维持 Segment 逻辑一致性的同时，最大化异构链路的并行吞吐能力。

## TENT 架构
TENT 通过解耦架构实现了从逻辑意图到物理执行的精密转化：应用层利用声明式 API 表达 Segment 为核心的存取意图与 SLO 约束，彻底屏蔽了底层硬件参数；编排层作为系统大脑，利用 Segment Manager 检索多维元数据（包含拓扑权重与异构协议凭证），在运行时通过“晚期绑定”动态求解最优传输计划；最终，统一传输后端层将任务细粒度切片，并基于实时遥测数据执行切片喷淋（Spraying），将数据流并行分发至 RDMA、NVLink 或 io_uring 等物理链路，确保物理执行在复杂非均质环境下能够确定性地满足上层业务的目标。

![](images/tent-internal-arch/tent-arch.png)

> 上图节选自我们近期发表的技术报告：https://arxiv.org/pdf/2604.00368

### 统一段表示
我们首先将 DRAM、HBM、Disk 全部抽象为 Unified Segment。对于上层应用（KVCache, Checkpoint）而言，数据不再存储在某个具体的“显存地址”或“文件路径”下，而是一个逻辑上的 Segment。TENT 内部也维护了 Segment Manager，用来记录 Segment 与底层物理介质的映射关系。

如下图所示，TENT 将物理上分散、异构的内存资源（虚拟地址空间）转化为系统可调度、可互操作的逻辑对象。与 Mooncake TE 一样，Segment 由一组 Buffer 组成，散落在不同 CPU 节点的 DRAM（`cpu:0`, `cpu:1`）以及 GPU 的 VRAM（`cuda:0`）中。它们被聚合进一个具有全局唯一标识的元数据对象（如 `name: node050:12345`）。应用层只需持有这个“逻辑句柄”，即可进行跨介质的操作。对于文件或者其它有独立地址空间的对象，由专门的 Segment 进行组织，格式是一样的。

![](images/tent-internal-arch/segment-metadata.png)

在实现中，Segment 的元数据通过三个关联分支，为编排器的自主决策提供了完整的信息支撑。

#### A. Buffers 维度：化解元数据不互认的“大杀器”
这是解决 NVIDIA、AMD 与国产卡混部的关键。

- 多协议凭证并存：注意看右侧 Buffers 框。在一个物理 Buffer 下，同时记录了其支持的 Transport 列表，如 `transports: [nvlink, rdma, tcp]`。

- 私有数据解耦（Transport Specific Data）：针对同一块内存，TENT 同时维护了 `nvlink/handle` 和 `rdma/rkeys` 等与具体 Transport 联系的东西。

```cpp
struct BufferDesc {
    uint64_t addr;
    uint64_t length;
    std::string location;
    std::vector<TransportType> transports;
    std::vector<Region> regions;
    std::unordered_map<TransportType, std::string> transport_attrs;
    // mutable elements
    int ref_count{0};
};
```

#### B. Topology 维度：调度优化的决策基础
图左侧展示了层级化的设备拓扑。根据物理距离（如 PCIe 拓扑、NUMA 亲和性），对于某种位置上的所有 Buffer，分别将硬件设备划分为 tier 1（极速路径）、tier 2、tier 3（跨 NUMA 或多级交换机）。编排器不再是盲目地随机选路，而是根据这个 Tier 列表计算不同路径的预期代价，从而在满足 SLO 约束的前提下，优先选择最优物理路径。

#### C. Devices 维度：硬件能力的标准化抽象
图下方展示了底层设备的物理属性。物理设备（如 mlx5_0）被抽象为具有特定 type（如 rdma）和协议元数据（lid, gid, props）的实体。不论底层是 Mellanox 网卡还是国产加速卡的私有互连协议，在这一层都被标准化，供传输引擎内部建立连接时调用。

需要指出的是，一般情况下上面要素都是自动检测生成的。当然我们也提供了灵活的配置文件进行干预（比如设置底层设备的黑白名单）。

### 统一传输后端
传输后端（Transport Backend）是 TENT 架构中直接与硬件驱动及网络协议栈交互的层级。其核心目标是为上层编排器提供一个多维度的物理能力池，并屏蔽不同传输协议在初始化、内存注册及数据搬运上的实现差异。

TENT 定义了一套统一的后端接口规范，使得不同性质的传输协议（如内存级的 NVLink、网络级的 RDMA、存储级的 io_uring）能够以插件化的方式接入系统。
- 资源发现与注册：后端在启动时会自动探测物理硬件（如网卡数量、GPU 拓扑位置），并将物理能力（带宽、延迟特性）反馈给 Segment Manager。
- 统一操作原语：无论是 RDMA 的 verbs 操作，还是文件系统的 read/write，在后端层都被抽象为针对 Segment Slice 的异步读写请求。

与 Mooncake TE 静态指定单一传输协议不同，TENT 的后端层支持一组（A set of）传输协议的并发运行。系统启动时根据环境自动激活所有可用的后端模块，不再依赖环境变量做排他性选择。后端层并不预设数据路径，而是等待编排器的 Transfer Plan。这种设计允许系统在同一个传输任务中，根据实时链路状态动态切换或并行使用多个后端（如同时利用 NVLink 和 RDMA）。

目前，我们已经实现了RDMA、NVLink （intra- and inter-node）、Ascend HIXL、TCP、io_uring、GDS、Buffered I/O 等 Transport，并且分别适配了 NVIDIA、AMD、昇腾、摩尔线程、沐曦等等国产平台。

### 编排器
TENT 的编排器（Orchestrator）承担了异构集群中的“中枢大脑”角色，其核心任务是将应用层抽象的声明式存取意图转化为精确的物理执行指令。在执行层面，编排器通过晚期绑定（Late Binding）机制实现了从“配置驱动”向“意图驱动”的跨越。它能够实时感测后端链路的遥测数据（Telemetry），并根据任务设定的 SLO 约束（如优先级）动态生成 Transfer Plan。无论是驱动 NVLink 进行单机 P2P 提速，还是调用基于 io_uring 的 File Backend 进行 GPU-SSD 直接存取，编排器都能在运行时自主决策最优的传输组合。这种自动化的编排能力不仅移除了 Mooncake TE 时代繁杂的参数配置负担，更通过切片喷淋与故障路径自动重定向，确保了大规模 AI 运行时状态流转的确定性满足。

目前对外发布的 TENT 编排器虽然实现了从命令式到声明式的跨越，但传输策略的决策逻辑仍然是硬编码的。在面对日益复杂的应用场景时，这种静态设计逐渐暴露出灵活性不足的问题。我们后续正在重新设计编排器的决策层，引入基于配置文件的策略引擎。敬请期待！
