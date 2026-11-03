---
name: abaqus-fea
description: "Abaqus 2024+ 有限元仿真自动化技能。当任务涉及 Abaqus/CAE 建模、网格划分、材料/截面、静力/模态/屈曲/显式动力学、接触、作业提交、ODB 后处理（应力云图/Mises/位移/探针）或 .cae/.inp 批处理时使用。通过 abaqus MCP 工具按需检索执行；模型与结果必须复核。触发词：Abaqus、CAE、有限元、FEA、网格、静力、模态、屈曲、显式、接触、ODB、Mises、云图、inp、悬臂梁、仿真。"
metadata: { "os": ["win32"], "requires": { "anyBins": ["python"] } }
---

# Abaqus 有限元自动化（abaqus-fea）

通过 `abaqus` MCP 服务器操控本机 Abaqus/CAE 2024+（2024/2025 已验证，Python 3.10）。
服务器处于 **discovery 模式**：会话里只有 `search_tools` 和 `call_tool` 两个元工具，
完整 24 工具目录按需检索，空闲约 280 token。

## 第 0 步：先问用户用哪种会话（必须）

**任何 Abaqus 任务开始前，先用提问工具让用户二选一，不要自行决定：**

1. `abaqus_status` 看会话与 `abaqusVersion`；若 `abaqusSupported=false`，
   停止并提示用户安装 Abaqus 2024+（2023 及更早默认不支持）。
2. 问用户：
   - **独立后台内核**：快、无窗口，适合批处理/只交付结果；
   - **手动打开的 GUI**：能在自己的 CAE 窗口里实时看到建模、网格、云图；
     需要装过一次 GUI 插件并重启 CAE；
3. 按选择启动：
   - 后台：`abaqus_session {"action":"start","mode":"background"}`
   - GUI（已打开 CAE）：`abaqus_session {"action":"start","mode":"gui"}`
   - GUI（让插件开一个可见窗口）：`{"action":"start","mode":"gui","launch":true}`
4. GUI 模式提示 “No Abaqus GUI bridge”：先
   `abaqus_session {"action":"install_gui"}`，让用户重启一次 CAE，再 `mode:"gui"`。

| 模式 | 关闭行为（`abaqus_finish`） |
|---|---|
| background | 内核进程退出 |
| gui + launch=true（插件开的窗口） | 保存后关闭该 CAE |
| gui（用户自己开的窗口） | 只停桥、**不关窗口** |

## 收尾：必须调用 `abaqus_finish`

任务完成（或用户中止）后，最后一步固定为：

```text
abaqus_finish {"save":"D:/Abaqus/work/项目名.cae"}
```

它会保存 `.cae`、关闭 ODB、结束会话。**不要**直接 `abaqus_session stop` 代替；
`finish` 才带关闭策略。忘记收尾会让 CAE 进程和 `.cae` 文件锁一直留着。

## 标准工作流（静力为例）

| 步骤 | 工具 | 关键点 |
|---|---|---|
| 建模 | `abaqus_part` | box / cylinder / cone / tube / revolve / shell / polygon / import |
| 材料 | `abaqus_material` | density、E、nu、plastic 表；单位与几何一致 |
| 截面 | `abaqus_section` | `part` 参数存在时自动指派 |
| 装配 | `abaqus_instance` | 平移/旋转；载荷作用在 **实例** 上 |
| 网格 | `abaqus_mesh` | `size` 种子 + `elem`；可加 `technique=free|structured|sweep`、`shape=tet|hex` |
| 分析步 | `abaqus_step` | static / frequency / buckle / explicit |
| 边界 | `abaqus_bc` | `Initial` 步固支；区域选择器见下 |
| 载荷 | `abaqus_load` | pressure 必须是 surface；集中力优先加到 RP |
| 耦合 | `abaqus_coupling` | 面→RP，力/弯矩加 RP；总力不再按节点翻倍 |
| 接触 | `abaqus_interaction` | general（Std 无摩擦）/ contact（可给 friction）/ tie |
| 作业 | `abaqus_job` | create → submit → wait/status；状态以 `.sta` 为准 |
| 检查 | `abaqus_inspect` | sets/surfaces 计数、loads/BCs/constraints 类型、单元类型 |
| 结果 | `abaqus_odb` | summary → extrema（按绝对值排序）→ probe |
| 出图 | `abaqus_plot` | GUI 模式抓真实窗口；后台用 matplotlib 云图 |

## 区域选择器

`region` 参数接受：

```json
"Fixed"                                                  // 已存在的 assembly set 名
{"set": "Fixed"}
{"surface": "Top"}
{"at": [[0, 10, 5]], "instance": "Beam-1"}               // 坐标必须落在面/边/体上
{"box": [[0,0,0],[100,20,10]], "instance": "Beam-1"}     // 包围盒内所有几何
```

- `at` 坐标取 **面中心** 最稳；落在棱上会选不中。
- 面选择用 `kind:"face"`（默认）；`abaqus_set` 可显式建 set/surface。
- 压力载荷会自动把面选择器转成 surface；其他载荷用 set。
- 集合建在 assembly 上，几何来自 instance；不要在 PartInstance 上直接 `Set`。

## 常见坑

1. **集中力加在整个面上是“每个节点一份”**：100 N 加到 25 个节点就是 2500 N，
   结果会大一个量级。正确做法是用 `abaqus_coupling`：
   `{"name":"TipRP","point":[100,5,5],"surface":{"at":[[100,5,5]],"instance":"Beam-1"},
     "force":{"cf3":-100},"step":"Step-1"}`，力加在 RP 上，总力就是 100 N。
2. **Part 与 Instance 几何不是一套**：载荷/边界/集合用 `rootAssembly` 或
   `instance` 的 faces；`part.faces` 只用于截面与网格。
3. **网格必须在 Part 上划**，且先 `seedPart` 再 `generateMesh`。
4. 固支/对称边界建在 `Initial`；载荷建在分析步。
5. `.cae/.odb` 路径含中文时，服务器会自动建 ASCII 目录联接；返回的是联接路径。
   旧版本（2023 及以前）的 .cae 无法被 2024+ 直接打开。
6. 一次 `abaqus_run_python` 只适合兜底；不要把整套建模塞进一段代码。
7. 每次建模后 `abaqus_model_info` 核对零件/实例/分析步/载荷是否真的进去了。
8. `abaqus_plot` 在 GUI 模式会激活 CAE 窗口并关闭“开始任务”页再截图；
   后台模式 `mode:"mpl"` 不弹窗。
9. **CAE 的 cwd 可能不是 `D:\Abaqus\work`**：`abaqus_status` 里
   `cwdMatchesWorkDir=false` 时，`abaqus_job write` 会自动把 `.inp` 复制到
   workDir；`submit` 也会自动补齐。不要再手工 `shutil.copyfile`。
10. **attach 手动 GUI 时 `finish` 不关窗口**，`.cae/.jnl` 仍被占用：交付物先
    复制到独立目录，不要删 `.jnl`；最终打包可用后台模式，或让用户关掉 CAE。
11. 常见 Abaqus 异常现在会附带 `Hint:`（旋转 profile、空选择、缺 inp、KeyError），
    先按提示修，不要盲目重试。

## 交付前自检

- 单位制与量级合理（钢梁 100 mm、100 N 端载，端挠度约 0.19 mm）。
- 网格收敛性：至少换一次 `size` 复算，极值变化 < 5%。
- 约束是否过度（刚体位移/奇异）检查 `.msg` 的数值奇异警告。
- 云图/极值图与边界条件方向一致；报告写清单位、网格量、最大 Mises 位置。

深入案例与 API 细节见 `references/workflows.md`、`references/units.md`、`references/api-notes.md`。
