# DFT Workflow Toolkit

面向研究者与其 agent 的通用 DFT 工作流工具。提供输入检查、执行状态与证据整理、静态交付、只读诊断、属性依赖规划、参数提案审阅和观察指标汇总。科学方法与最终科学接受由研究者负责。

本仓库适合希望逐步建立可追溯计算流程的团队。工具不提供默认研究参数，也不会通过规划或审阅自动授权计算。公开内容只包含通用源码、文档、空白模板和合成测试。

## 第一次使用

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
| 工作流观察指标 | `tools/workflow_metrics.py` | 只汇总显式列出的回执；缺少可比证据时不宣称提速 |
| 管理记录检查 | `tools/workflow_check.py` | 当前限制 MPI-only、最多 64 ranks；源码锁检查不是通用依赖管理 |

查看新增工具参数：

```sh
python -B tools/tool_evidence.py --help
python -B tools/property_plan.py --help
python -B tools/parameter_advice.py --help
python -B tools/workflow_metrics.py --help
```

`approved_bundle.py` 是从维护源生成的契约校验函数，仅供参数审阅使用。公开版不包含工作站专用准备后端、总入口、能力目录或真实环境配置。

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

其他工具按各自 `--help` 接受明确路径，不能假定都支持上述环境变量。公开版远端示例根目录为 `/srv/dft/calculations/<batch>`；部署者须审阅适配自己的批准目录并验证拒绝行为。

## 让你的 agent 接手

将 [AGENTS.md](AGENTS.md) 与任务交接一起交给 agent。交接至少提供：目标、来源路径、批准规格、允许读写位置、允许的本地或远端动作、验收标准、停止条件。

推荐顺序是读取指定证据、列明缺口、执行批准范围内的准备或分析、运行相关检查、交付实际路径与回执。缺少科学选择时回到研究者；证据不足时保留 UNKNOWN/PARTIAL。默认不启动计算、不联网传输、不持续监控，也不自行授予权限。

## 验证与贡献

安装科学依赖后运行公开合成测试：

```sh
python -B -m unittest discover -s tools -p 'test_*.py'
python -B scripts/check_public_release.py --require-repository
```

测试使用构造的数据，不运行 VASP 或连接工作站。修改时先运行受影响测试，发布前验证公开套件、依赖与内容边界。边界检查不是科学验收，也不能识别所有未发表的思想或未知格式的密钥。

本仓库由维护者的私有开发源按显式清单生成。维护者在源仓库修改并同步发布；外部使用者可以通过 issue 或 PR 提交通用改进，维护者需纳入维护源后重新生成。不要提交真实文献、论文、研究想法、材料结构、计算输入输出、回执、主机信息或密钥。

`release_manifest.json` 记录经审阅文件及完整性摘要；新增文件须明确审阅后扩展清单。许可证尚未指定，使用与再分发前请向维护者确认授权。

## 进一步阅读

- [协作与科学证据边界](docs/workflow.md)
- [私有工作区结构](docs/private_workspace.md)
- [公开边界与贡献要求](docs/publication_policy.md)
- [依赖与第三方许可](THIRD_PARTY.md)
