# 显式 JSON 内容核验

`tools/evidence_verifier.py` 是独立标准库入口。它读取显式清单中的本地 JSON，比较指定字段和身份，保留缺口；不会扫描目录、访问 URL、准备输入或授权计算。通用 `vasp_core_cli.py` 仍是五种模式，此工具独立调用。

```sh
python -B tools/evidence_verifier.py --help
python -B tools/evidence_verifier.py --workspace ../toolkit-private-trial --manifest manifest.json --output reports/verification.json
```

退出码 0 表示清单内客观检查全部匹配；1 表示已经写出含缺口或不匹配的回执；2 表示合同、路径、输出或读取错误使操作停止。匹配不是收敛、方法合理性、科学接受或执行批准。

## 合成试用

从工具库根目录执行下列 Python 片段，在旁边创建新的私有试用目录；已有目录会使操作停止。示例字段仅用于测试程序。

```python
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tools'))
from evidence_verifier import digest
workspace = Path.cwd().parent / 'toolkit-private-trial'
workspace.mkdir(exist_ok=False)
(workspace / 'sources').mkdir()
(workspace / 'reports').mkdir()
(workspace / 'sources/state.json').write_text(json.dumps({
    'schema': 'vasp-execution-state/v1', 'execution': 'synthetic-only'}), encoding='utf-8')
manifest = {
    'schema': 'vasp-evidence-manifest/v1',
    'subject': {'kind': 'synthetic', 'sha256': digest({})},
    'files': [{'id': 'state', 'path': 'sources/state.json',
               'allowed_schemas': ['vasp-execution-state/v1']}],
    'checks': [{'id': 'execution-match', 'identity': 'execution',
                'left': {'file': 'state', 'pointer': '/execution'},
                'expected': 'synthetic-only'}]}
(workspace / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
```

随后运行上面的核验命令。检查 `reports/verification.json` 的匹配状态、文件身份和 false 授权字段。修改源 JSON 的 `execution` 后重新核验，应得到不匹配；旧回执在消费者中也会因重新核对内容而失效。

## 清单与回执合同

清单必须有非空 `files`、`checks` 和绑定主体的 `subject`。文件 id 唯一，路径在显式工作区内，schema 必须支持且在该文件的 `allowed_schemas` 中。每项检查有唯一 id、identity 类别（execution/case/attempt/source/environment/version）、`left` 引用，并且只提供 `expected` JSON 值或 `right` 引用之一。引用只能指向列出的文件。

支持的 schema：`vasp-approved-bundle/v1`、`vasp-practical-preparation/v1`、`vasp-executor-check/v1`、`vasp-execution-state/v1`、`vasp-tool-evidence/v1`、`toolkit-clean-science-validation/v1`。支持标签不等于完整 JSON schema 验证。已有明确 schema 不能被映射覆盖；旧格式映射仅接受无 schema 头、明确整数 `schema_version: 1` 的 v1 合同，并须显式声明 pointer、expected 和 schema。

指针遵循 RFC 6901，支持空指针及 `~0`/`~1` 转义；数组下标严格。JSON 类型和值精确比较：bool、int、float 不互换，不使用数值容差。重复键和非有限数拒绝。未知 schema、缺文件/字段、指针错误、类型或身份冲突分别保留状态。

`vasp-evidence-verification/v1` 回执绑定主体、清单、核验器版本和读取源的规范化 JSON 身份。仅空白变化不改变语义身份；内容、要求或主体变化需重新核验。每个匹配只证明显式检查的字段，不能自动证明所有输入来源的对应关系。

输出文件必须不存在，父目录必须已建立；不能覆盖、写入源/清单的直接目录或任何计算树。独立 `reports/` 子目录可以使用；计算目录中的 workspace 也不能绕过保护。路径越界与链接边界受守卫控制，但 Windows 链接权限导致的跳过项不能声称已经执行验证。

## 规划与参数建议的可选接入

`property_plan.py` 和 `parameter_advice.py` 必须同时提供 `--evidence-manifest`、`--evidence-receipt` 才消费核验。主体分别绑定完整 request（kind=`property-plan-request`）或 `{bundle, proposal}`（kind=`parameter-advice-inputs`）。使用 `digest()` 计算相应规范化对象身份；调用者仍须在清单中明确声明所需 execution/case/attempt/source/environment/version 对应关系。

消费者重新读取显式清单中的 JSON 并核对回执，不把存在路径升级为全部内容已审阅。匹配信息单独附在 `objective_evidence_verification` 中；既有 blockers、建议状态、gate、execution_authorized 和科学接受状态保持不变。不提供成对选项时，原默认行为保持。
