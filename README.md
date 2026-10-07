# DFT Workflow Toolkit

通用 DFT 工作协作与 VASP 本地工具。内容包括输入校验、执行状态、证据解析、静态交付包构建，以及结果记录。工具报告事实与机械检查结果，科学接受由研究者决定。

本仓库仅包含通用文档、工具源码和合成测试。真实文献库、论文、研究方案、材料结构、计算输入输出、工作站配置与运行记录由使用者在独立私有工作区维护。

## 快速开始

Python 3.10 或更新版本；基础校验、解析和静态构建只使用标准库。几何检查需要 [requirements-science.txt](requirements-science.txt) 中的依赖。附加分析按需安装 [requirements-analysis.txt](requirements-analysis.txt) 中的依赖。安装依赖由使用者选择自己的隔离环境完成。

在仓库根目录运行：

```text
python -B scripts/check_public_release.py --require-repository
python -B tools/vasp_input_generator.py --help
python -B tools/vasp_executor.py --help
python -B tools/progress_snapshot.py --help
python -B tools/static_delivery_builder.py --help
python -B tools/result_registry.py --help
```

安装科学依赖后，可运行全部合成测试：

```text
python -B -m unittest discover -s tools -p "test_*.py" -v
```

测试仅使用构造的数据，不调用 VASP，不连接工作站。测试中的 A/B、Si/O 标签、坐标、能量和参数为程序测试夹具，不能用于科研计算。

## 使用入口

| 工具 | 用途与边界 |
|---|---|
| `vasp_input_generator.py` | 根据明确规格生成本地候选输入，并核对实际生成文件；保留源结构身份与约束 |
| `vasp_executor.py` | 本地输入 preflight 与输出 postcheck；分别报告程序结束、证据完整性、收敛状态 |
| `progress_snapshot.py` | 解析本地输出、离子/电子步、力与计时证据 |
| `progress_evidence.py` | manifest 绑定的证据整理与一次只读状态查询；远端动作需要单独授权 |
| `vasp_operations.py` | 状态与有界操作计划；默认 dry-run，真实传输 adapter 由使用者另行提供 |
| `static_delivery_builder.py` | 从指定源帧和批准规格生成本地静态交付包；默认关闭执行批准门 |
| `static_delivery_check.py` / `static_runtime_guard.py` | 核对交付身份、模板、实际输入与运行边界 |
| `vasp_execution_state.py` / `job_watch.py` | 执行状态及显式部署的作业监督机制；作业监督会运行配置命令，只应在授权环境使用 |
| `result_registry.py` | 不可变 JSON 结果记录与可重建 SQLite 索引；具体结果留在私有工作区 |
| `practical_geometry.py` / `scientific_helpers.py` | 几何校核、显式原子选择与能量记录比较 |
| `analysis_addons.py` | 按需诊断、显式窗口平均、总 DOS 与 HDF5 摘要；需要附加依赖 |
| `workflow_check.py` | 记录完整性检查；当前版本限制为 MPI-only、最多 64 ranks。源码锁检查兼容固定参考集合，不是通用依赖管理器 |

公开版只读远端证据路径约定为 `/srv/dft/calculations/<batch>`。这个目录是软件的示例部署约定，不提供实际主机、账号或环境身份。部署者须按自身批准目录调整两个证据/交付检查器并重新验证路径拒绝行为。

`templates/task_record.template.json` 是空白管理记录，不是输入生成器的批准规格。其 DRAFT 状态应被记录检查器拒绝。生成输入必须自行提供完整、经研究者确认的物理值和真实来源；没有自动补齐研究参数的流程。

## 文档

- [协作与验证流程](docs/workflow.md)
- [私有工作区结构](docs/private_workspace.md)
- [公开边界与贡献要求](docs/publication_policy.md)
- [软件依赖与许可说明](THIRD_PARTY.md)

## 当前发布状态

本仓库从拥有者的私有开发工作区按固定白名单生成。代码的维护与验证在私有开发工作区完成，公开库记录通用工具的发布变化；请勿直接维护另一份源码。本项目代码许可证尚未指定。未包含第三方源码、授权赝势或商业软件。当前没有预置真实工作站配置，也不包含自动提交计算的默认入口。

`release_manifest.json` 锁定本次审阅的文件与 SHA-256。文件有变化时先审阅新增内容，再更新清单；检查器不会自动批准或自动扩展清单。自动检查不能识别所有尚未发表的思想，公开前必须由拥有者确认文本与方法边界。
