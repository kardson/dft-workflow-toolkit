# DFT Workflow Toolkit

面向研究者与其 agent 的 DFT 工作流工具，当前计算工具主要面向 **VASP**。提供输入检查、执行状态与证据整理、静态交付、只读诊断、属性依赖规划、参数提案审阅和观察指标汇总。科学方法与最终科学接受由研究者负责。

本仓库适合希望逐步建立可追溯计算流程的团队。工具不提供默认研究参数，也不会通过规划或审阅自动授权计算。公开内容只包含通用源码、文档、空白模板和合成测试。

## 适用软件与版本

当前公开工具主要面向 **VASP**。通用协作规则可复用；CASTEP、Materials Studio 和 Quantum ESPRESSO 的专用输入/输出与运行后端尚未提供。

| 软件 | 参照版本 | 证据与使用范围 |
|---|---|---|
| VASP | **6.5.1** | 开发环境配置参照；处理 VASP 输入、输出与结果证据，不等于其它版本已兼容 |
| VASPKIT | **1.5.1** | 既有集成记录参照；公开工具不附工作站调用配置 |

Python 源码语法要求为 **3.10+**。这不代表所有解释器和第三方依赖组合均已验证。

### 已记录安装环境

科学工具环境 Python **3.12.14**；分析环境 Python **3.12.10**。下表由安装版本记录生成，不是最低版本声明或完整安装锁。

| 包 | 科学工具环境 | 可选分析环境 |
|---|---|---|
| numpy | 2.5.3 | 2.5.3 |
| ase | 3.29.0 | 3.29.0 |
| pymatgen | 2026.9.24 | 2026.9.24 |
| pymatgen-core | 2026.9.23 | 2026.9.23 |
| matplotlib | 3.11.2 | 3.11.2 |
| custodian | 未记录 | 2025.12.14 |
| py4vasp | 未记录 | 未记录 |
| py4vasp-core | 未记录 | 0.11.3 |
| sumo | 未记录 | 3.0.0 |
| MacroDensity | 未记录 | 3.1.0+source.a9b56cce |

历史公开合成测试基线为 `563e1ec`：56 项，3 项平台相关跳过。此结果仅覆盖该提交的工具快照；本表的安装观察不自动成为新代码或全部分析功能的验收。

公开快照 `3c6e990` 在干净 Python **3.12.14** 标准库环境中通过文件守卫、四个帮助入口及三个合成私有工作区 CLI 验证；没有安装科学或分析依赖，未生成计算输入或授权执行。此证据不覆盖 O2 新实现。

本地 O2 工具候选（身份 `23eee01417f9`）通过公开合成套件 56 项（3 项平台相关跳过）；新增通用 CLI 的 4 项集成测试分别在科学依赖环境和 without-pip 标准库环境通过。覆盖能力目录、私有操作拒绝、工作区越界和缺失模块拒绝；不覆盖科学依赖的干净安装、实际 VASP 运行或全部分析功能。此记录是本地候选证据，不是远端发布记录。

新的隔离科学环境在 **Windows / AMD64 / Python 3.12.14** 上完成安装与依赖检查：公开工具 53 项通过、3 项符号链接权限跳过，通用 CLI 4 项通过，Si/O 合成几何调用通过。只覆盖记录指定的工具快照及安装组合，不包含真实 VASP 计算。见 [机器可读兼容记录](docs/compatibility-clean-science.json) 和 [复现步骤](docs/reproduce-clean-science.md)。

Linux/macOS 完整套件、全部可选分析功能、其它 VASP/Python/依赖版本及跳过的链接边界尚未验证。新增实际验证后，应更新兼容记录并重新生成本节。

只用标准库工具时无需安装科学或分析依赖。按需使用 `requirements-science.txt`、`requirements-analysis.txt`；MacroDensity 的来源与构建需另行核对。VASP、授权赝势与工作站配置不随仓库提供。工具许可证尚未指定，第三方软件遵循各自许可，见 [第三方说明](THIRD_PARTY.md)。

## 第一次使用

首次本地试用可直接复制 [完整合成规划请求](examples/synthetic-property-request.json)，无需从测试代码猜字段。它故意缺少科学合同，预期退出码为 0、结果为 `BLOCKED`、10 项证据缺口，且执行授权和 gate 均为 false；不会生成 VASP 输入。

从工具库根目录运行以下 PowerShell 示例，工作区在旁边新建，已有目录即停止：

```powershell
$toolkitRoot = (Get-Location).Path
$trialWorkspace = Join-Path (Split-Path $toolkitRoot -Parent) 'synthetic-first-plan'
if (Test-Path -LiteralPath $trialWorkspace) { throw '请选择新目录，保留已有工作' }
New-Item -ItemType Directory -Path $trialWorkspace | Out-Null
Copy-Item -LiteralPath (Join-Path $toolkitRoot 'examples/synthetic-property-request.json') -Destination (Join-Path $trialWorkspace 'request.json')
$env:DFT_WORKSPACE_ROOT = $trialWorkspace
python -B (Join-Path $toolkitRoot 'tools/property_plan.py') --request (Join-Path $trialWorkspace 'request.json') --output-dir (Join-Path $trialWorkspace 'plan')
```

查看 `plan/property_plan.json`、`plan/blockers.json` 和 `plan/dependency_table.csv`。`BLOCKED` 表示需要补实际批准的来源与证据；不要删除 blocker 或把合成合同当成研究批准。真实使用仍需下面的任务交接和私有工作区约束。

1. 将本仓库克隆到工具目录，准备 Python 3.10 或更新版本。
2. 阅读本页和 [AGENTS.md](AGENTS.md)，再按任务阅读 [协作流程](docs/workflow.md)。
3. 为真实研究建立独立私有工作区，明确来源、目标、允许动作和停止条件。
4. 先查看相应工具的 `--help`；用显式规格执行一次本地任务，再检查生成的回执。

从仓库根目录检查公开文件边界：

```sh
python -B scripts/check_public_release.py --require-repository
```

基础解析、契约检查、证据摘要及新增规划工具只依赖标准库。需要几何与科学解析时，在自己的隔离环境安装：

```sh
python -m venv .venv
# 激活该环境后：
python -m pip install -r requirements-science.txt
```

附加诊断、DOS、HDF5 等分析按需安装 `requirements-analysis.txt`。MacroDensity 需另行取得。这里未固定所有依赖版本；请为实际研究记录并验证自己的软件环境。

## 按任务选择入口

通用校验入口为 `tools/vasp_core_cli.py`，与能力表共用命令目录。先查看帮助和当前安装闭包：

```sh
python -B tools/vasp_core_cli.py --help
python -B tools/vasp_core_cli.py capabilities
python -B tools/vasp_core_cli.py --workspace /path/to/private-workspace validate-bundle --bundle approved-bundle.json
```

该入口提供 `validate-bundle`、`preflight`、`postcheck`、`evidence`、`capabilities` 五种模式。文件参数必须在所选工作区内；默认拒绝私有批准配置及 `LREAL=Auto`。它不提供准备或提交计算的命令，校验通过也不构成执行授权。缺失模块的命令不会被能力表列为可用。其他分析与规划工具仍按下表独立调用。

各工具独立运行，不需要原开发者的总入口或工作站配置。

| 任务 | 入口 | 输出与边界 |
|---|---|---|
| 检查或生成本地候选输入 | `tools/vasp_input_generator.py` | 使用明确规格；准备完成不等于已提交 |
| 执行前后检查 | `tools/vasp_executor.py` | 区分程序结束、证据完整性和收敛状态 |
| 整理本地进度 | `tools/progress_snapshot.py` | 解析步数、力、计时及缺失证据 |
| 有界远端证据查询 | `tools/progress_evidence.py` | 需要显式 manifest 和独立远端授权 |
| 操作计划与作业监督 | `tools/vasp_operations.py`、`tools/job_watch.py` | 监督可运行配置命令，使用前明确批准范围 |
| 静态交付 | `tools/static_delivery_builder.py` | 默认关闭执行批准门；配合交付检查与运行守卫 |
| 保存结果记录 | `tools/result_registry.py` | 不可变 JSON 与可重建索引，实际结果留在私有区 |
| 几何与显式参照比较 | `tools/scientific_helpers.py` | 报告事实，不替研究者选择参照 |
| 附加分析与诊断分类 | `tools/analysis_addons.py`、`tools/diagnostic_taxonomy.py` | 只读诊断及候选分析，不自动修复或重启 |
| 工具回执摘要 | `tools/tool_evidence.py` | 读取指定 JSON 回执，生成证据 sidecar |
| 属性依赖规划 | `tools/property_plan.py` | 生成依赖计划和阻塞项，不生成计算输入或启动计算 |
| 参数提案审阅 | `tools/parameter_advice.py` | 对照原批准契约检查变更，不自动接受提案 |
| 显式 JSON 内容核验 | `tools/evidence_verifier.py` | 只比较清单指定字段，输出匹配与缺口；见[核验合同与合成试用](docs/evidence-verifier.md) |
| 工作流观察指标 | `tools/workflow_metrics.py` | 只汇总显式列出的回执；缺少可比证据时不宣称提速 |
| 管理记录检查 | `tools/workflow_check.py` | 当前限制 MPI-only、最多 64 ranks；源码锁检查不是通用依赖管理 |

查看新增工具参数：

```sh
python -B tools/tool_evidence.py --help
python -B tools/property_plan.py --help
python -B tools/parameter_advice.py --help
python -B tools/workflow_metrics.py --help
```

`approved_bundle_validation.py` 是完整同源的契约校验模块，`approved_bundle.py` 是其公开薄入口。公开版包含五模式通用入口和能力目录；工作站专用准备后端、私有总入口和真实环境配置保留在私有项目。校验通过不授权准备或提交计算。

## 私有工作区与实际任务

源码目录与真实研究数据分开。规划和指标工具通过 `DFT_WORKSPACE_ROOT` 选择私有工作区；未设置时使用工具仓库根目录，仅适合合成试用。处理真实材料前明确设置该变量，输入引用与输出目录必须满足工具的工作区边界检查。命令行相对路径以当前目录解析；建议像下方示例一样传绝对路径。计算目录使用 `private_runs/`，规划和报告不能写入该目录。

PowerShell：

```powershell
$env:DFT_WORKSPACE_ROOT = '/path/to/private-research'
```

Linux/macOS：

```sh
export DFT_WORKSPACE_ROOT=/path/to/private-research
```

下面只示范命令结构。JSON 需由研究者与 agent 按实际来源准备，路径需替换；不要把真实数据放入公开库：

```sh
python -B tools/tool_evidence.py --receipt /path/to/private-research/receipts/result.json
python -B tools/property_plan.py --request /path/to/private-research/requests/properties.json --output-dir /path/to/private-research/reports/new-plan
python -B tools/parameter_advice.py --approved-bundle /path/to/private-research/requests/approved.json --proposal /path/to/private-research/requests/proposal.json --output-dir /path/to/private-research/reports/new-review
python -B tools/workflow_metrics.py --observations /path/to/private-research/observations/manifest.json --output-dir /path/to/private-research/reports/new-metrics
```

规划、审阅和指标输入分别使用 `vasp-property-request/v1`、`vasp-parameter-proposal/v1`、`vasp-workflow-observations/v1`。参数审阅还需要 `vasp-approved-bundle/v1`。合成测试展示这些契约的构造和拒绝样例；其中参数与数值只用于程序测试。输出目录必须是新的，保护性检查会拒绝计算目录或不允许的来源。

规划和参数审阅可以同时指定 `--evidence-manifest` 与 `--evidence-receipt`，消费 `vasp-evidence-manifest/v1` 与 `vasp-evidence-verification/v1`。主体、清单或源内容变化使旧核验失效；核验只附加客观匹配，不改变原 blocker、建议状态、执行 gate 或科学授权。不提供成对选项时保持原默认行为。

运行规划或参数审阅时，先切换到独立私有工作区，再用工具目录的完整路径调用脚本，并将 `DFT_WORKSPACE_ROOT` 设置为该私有工作区。CLI 的相对输入参数按当前目录解析；设置环境变量不会自动切换目录。也可以给输入参数使用工作区内的绝对路径。试用产生 `BLOCKED` 回执仍可表示程序正常完成：继续查看实际 blocker，不能把它改成执行批准。

其他工具按各自 `--help` 接受明确路径，不能假定都支持上述环境变量。公开版远端示例根目录为 `/srv/dft/calculations/<batch>`；部署者须审阅适配自己的批准目录并验证拒绝行为。

## 让你的 agent 接手

将 [AGENTS.md](AGENTS.md) 与任务交接一起交给 agent。交接至少提供：目标、来源路径、批准规格、允许读写位置、允许的本地或远端动作、验收标准、停止条件。

推荐顺序是读取指定证据、列明缺口、执行批准范围内的准备或分析、运行相关检查、交付实际路径与回执。缺少科学选择时回到研究者；证据不足时保留 UNKNOWN/PARTIAL。默认不启动计算、不联网传输、不持续监控，也不自行授予权限。

## 验证与贡献

安装科学依赖后运行公开合成测试：

```sh
python -B -m unittest discover -s tools -p 'test_*.py'
python -B -m unittest discover -s tests -p 'test_*.py'
python -B scripts/check_public_release.py --require-repository
```

`tools/` 包含工具与内容核验器的合成回归；`tests/` 包含通用 CLI 集成和独立诊断夹具消费测试。两组分别运行，不能只运行其中一组就宣称完整验证。具体计数与跳过原因以所验证工具快照的回执为准；诊断检查缺少指定可选依赖时会明确跳过。

测试使用构造的数据，不运行 VASP 或连接工作站。修改时先运行受影响测试，发布前验证公开套件、依赖与内容边界。边界检查不是科学验收，也不能识别所有未发表的思想或未知格式的密钥。

本仓库由维护者的私有开发源按显式清单生成。维护者在源仓库修改并同步发布；外部使用者可以通过 issue 或 PR 提交通用改进，维护者需纳入维护源后重新生成。不要提交真实文献、论文、研究想法、材料结构、计算输入输出、回执、主机信息或密钥。

`release_manifest.json` 记录经审阅文件及完整性摘要；新增文件须明确审阅后扩展清单。许可证尚未指定，使用与再分发前请向维护者确认授权。

## 进一步阅读

- [协作与科学证据边界](docs/workflow.md)
- [私有工作区结构](docs/private_workspace.md)
- [公开边界与贡献要求](docs/publication_policy.md)
- [依赖与第三方许可](THIRD_PARTY.md)

诊断支持范围见 [逐例验证与缺口记录](docs/diagnostic-validation-scope.json)。真实非命中、XML 解析观察与独立合成分支分开记录；合成阳性不能解除真实样本门槛。当前默认 BRMIX 不变，新检测器未启用，不报告总体准确率。

工作流计量字段和缺失规则见 [观察合同](docs/workflow-observation-contract.json)。墙钟、人操作时间、首次通过与机械返工分开；未测量保留空值，历史启动快照不能当作当前任务完成。没有同类可比组时不报告改善率。内部合成试用不等于外部首次使用者或节时验证。

