# IncFinder usage

WSL2 的输出目录放在发行版自己的 Linux 文件系统中，例如 `~/incfinder-results`。在本次配置中，向 Windows 挂载盘发布已计算的目录时，DrvFS 返回了 EINVAL。Docker 可用 Linux 数据卷保存结果。


默认在 Windows Git Bash / Linux Bash 中使用。核心计算语言为 Python；`incfinder` 是 Python 标准控制台入口。

```text
Usage:
  incfinder <command> [options]
  incfinder [prediction options]

Commands:
  predict    Predict Inc labels from FASTA/FASTQ/gzip
  doctor     Check dependencies, resources and model location
  check      Verify software resources and model SHA-256
  version    Show version
  help       Show general or command-specific help
```

无参数运行显示帮助并退出。`incfinder help predict` 与 `incfinder predict --help` 等价。帮助和版本不加载科学计算库，不依赖模型配置。


## 模型校验、安装与续传

完整校验使用 `incfinder models verify --model-dir ./models`。如需同时核对 RF 的 Stage 1 文件和 Stage 2 的 22 卷，追加 `--parts-dir ./model_assets`。

```bash
incfinder models install --model-dir ./models --parts-dir ./model_assets --ntv3-dir ./ntv3_files
```

安装默认离线。分卷按 V1 发布清单检查大小与 SHA-256，拼接后的完整文件再与冻结清单对照。安装先在目标所在卷暂存，所有缺件完整核对后，再逐个以原子操作写到最终文件名。已有正确文件直接复用，身份不符的已有文件会报错并保留。中断后用原命令重试，可继续使用已验证的分卷及完整暂存文件。

`--download-rf` 和 `--download-ntv3` 分别明确允许通过已登录的 GitHub 和 Hugging Face 会话获取缺件。授权在各平台完成。`--cache-dir` 可将续传缓存放在另一磁盘，拼接暂存区仍在目标所在卷。空间不足会给出磁盘与所需、可用字节。日志及 JSON 收据登记大小、摘要和来源，不记录凭据和带签名的下载地址。

## predict

```bash
incfinder predict examples/sample.fasta -o results/run1 --model-dir models --allow-encoder-drift
incfinder predict -i examples/sample.fastq -o results/run2 --model-dir models --allow-encoder-drift
incfinder --i examples/sample.fasta --o results/run3 --allow-encoder-drift
```

| 参数 | 含义与默认值 |
|---|---|
| 位置输入或 `-i / --i / --input` | 一个文件，两种写法不能同时指定；支持 gzip |
| `-o / --o / --output` | 尚不存在的输出目录；不会覆盖已有结果 |
| `--model-dir` | 包含 ntv3/、rf/ 的完整模型目录 |
| `--device` | 默认 cpu；Conda 环境具有合适 GPU 依赖时才可显式选 cuda；本轮只验证 CPU |
| `--threads` | 编码线程，默认 4 |
| `--jobs` | RF 任务数，默认 1 |
| `--batch-size` | 编码批大小，默认 1 |
| `--threshold-mode` | 默认 ct；可选原有 u05，不以本轮测试重选阈值 |
| `--features-dir` | 已有 features.npy 和绑定身份的 features.json；须匹配 ID、顺序、格式、序列摘要及合同 |
| `--allow-encoder-drift` | 明确接受编码等价尚未确立；新序列研究预览必须指定 |
| `--max-length` | 默认 10000；不静默截断 |
| `--allow-out-of-domain` | 明确接受超出原长度范围的外推；不表示该范围已验证 |
| `--parent-run-id` | 可选。关联同一记录目录中已结束的 Run |

输入、输出、特征和显式模型相对路径均按调用者当前目录解析。位置参数 `-` 被拒绝，本版本不提供 stdin/管道输入。Windows 含空格或中文的路径使用双引号。退出码 0 表示命令执行成功；参数或运行错误为 2。编码科学状态保存在结果中，命令退出码 0 不代表原编码等价通过。

## 运行记录与复现

每次预测使用一个新 UUID。成功后通过返回值和 `run_summary.json` 的 `run_id` 获取，失败后通过 CLI 提示或 Python 异常的 `run_id`、`record_path` 获取。`from incfinder import load_run_record` 后可按 ID 读取记录。路径为 `<记录目录>/<run_id>/run.json`。

Windows 默认使用 `%LOCALAPPDATA%/IncFinder/runs`。Linux 使用绝对路径形式的 `$XDG_STATE_HOME/incfinder/runs`，默认回退到 `~/.local/state/incfinder/runs`。环境变量 `INCFINDER_RUN_DIR` 可选择可写的记录目录。Docker 包装器将记录保存在宿主机输出父目录的 `.incfinder-runs/`。

- `parameters` 保存生效值及 `explicit` 或 `default` 来源。记录还包括输入 SHA-256、特征来源及摘要、解释器、软件、科学合同、输出合同和经校验的模型资产清单 SHA-256。
- `status` 使用 `running`、`succeeded`、`preflight_failed` 和 `execution_failed`、`interrupted`。失败记录保存当时已解析或验证的字段、出错步骤和原因代码。记录只接收定义好的字段，疑似带凭据的路径整项脱敏。工作进程日志保存模块与退出码。
- 复现时为新预测提供 `--parent-run-id`（CLI 或旧入口）或 `parent_run_id`（Python API）。使用同一记录目录和新的输出目录。根据已有记录选择需要的参数，子 Run 保有自己的输入、参数和摘要。
- 同一个输出路径一次只允许一个预测，并在最终发布时保护已有路径。进程被强制终止后可能留下 `running` 记录及含 Run ID 的 `.incfinder-output-*.lock`。只有确认对应进程退出后才可手动移除该占用文件。

### 事件查询与中断对账

新 Run 的 `run.sqlite3` 与 `run.json` 位于同一目录。主进程以 SQLite 事务追加事件并更新状态投影。事件记录 Run ID、attempt ID、顺序、UTC 时间、类型、版本和幂等键。`load_run_record` 从已提交的事件读取当前状态。IF-006 创建的历史 JSON 仍可按原方法读取。

```python
from incfinder import load_run_events, rebuild_run_state, reconcile_run

events = load_run_events(run_id)
state = rebuild_run_state(run_id)
reconciled = reconcile_run(run_id)
```

重建和对账需要该 Run 的写进程退出。重建恢复 SQLite 投影和 `run.json`。对账依据提交的事件和磁盘产物判断终态。如果没有可核实的完整发布，则追加 `interrupted`。如果结果已经发布且 Run ID、摘要和已登记产物的 SHA-256 均匹配，则记录已核实的成功发布。重复对账沿用已提交终态。对账不会清理结果占用文件。

### 批次检查点、取消与续算

由 IncFinder 编码的预测在本地 Run 目录中保存批次检查点。每批写入临时文件，核验大小、SHA-256、形状与有限的 FP32 值后发布，再用 SQLite 事务登记。组装特征时逐批重验，并按输入行序填入。按 Ctrl+C 可回收本次预测的子进程并登记 `cancelled`，CLI 退出码为 130。

```python
from incfinder import predict

predict(input="input.fasta", output="resumed", allow_encoder_drift=True,
        resume_run_id="previous-run-id")
```

CLI 参数为 `--resume-run-id RUN_ID`，需要原输入、生效参数、相同模型与编码环境，以及新的输出目录。续算创建新 Run ID，并自动关联已取消、中断或执行失败的旧 Run。活跃写者、身份不符及无法核验的批次登记会拒绝续算。已登记且文件核验通过的批次可复用，未登记、缺失或损坏的批次会重算。RF 和报告从完整的特征重新生成。

检查点与 SQLite 登记保存在旧 Run 的 `checkpoints/` 中。结果目录仍使用原有发布流程。直接提供外部特征时沿用现有预计算路径。编码等价性以现有科学合同为准。

### 产物清单与归档

新发布的预测会生成 `artifact_manifest.json` 和 `artifact_manifest.sha256`。清单递归登记结果、元数据、嵌套日志和内部特征，记录各文件的字节数、SHA-256、阶段、保留方式和直接父依赖。原始输入和预计算特征默认记录原路径与摘要。模型身份引用冻结资产清单的摘要。

```python
from incfinder import export_artifact_archive, verify_artifacts

verify_artifacts("example-result")
export_artifact_archive("example-result", "example-archive-reference")
export_artifact_archive("example-result", "example-archive-copy", external_mode="copy")
verify_artifacts("example-archive-copy")
```

引用归档校验每个外部文件的原路径和内容。复制归档包含原输入和外部特征文件对，移动归档目录后可独立校验。两种归档均独立发布到新目录，模型权重与实时 SQLite 事件库由原位置维护。验证时可通过 `expected_manifest_sha256` 传入另行保管的清单摘要。涉及敏感路径的引用需要通过 `reference_paths={artifact_id: path}` 显式提供实际路径。

已有的预测结果仍可按原有接口读取。归档验证需要新预测中生成的递归清单。

### 查看与导出血缘

血缘入口只读核验已完成的结果目录或已封存归档。文件图登记变换活动与直接依赖。记录图关联原始输入行、特征、实际执行的分类阶段、阈值和输出策略。门控拒绝、通过门控后无标签以及已调用标签分别显示。记录以零基行号和序列摘要定位。输出保留原有科学结果的身份。

```bash
incfinder lineage example-result
incfinder lineage example-result --row 3
incfinder lineage example-archive-copy --output example-lineage --start 0 --stop 10 --page-size 5
```

Python 可用 `inspect_lineage(directory, row_index=3)` 和 `export_lineage(directory, destination, start=0, stop=10)`。导出默认从起点取至多一页，范围终点排除在外。每页最多 100 行。首页 `index.html` 只显示批次汇总及文件关系，行明细从相对链接打开。离线页面与 JSON 一起移动后可继续浏览。导出前会核验完整源结果。

`--manifest-sha256` 可绑定另行保管的清单摘要。对于经脱敏的引用，可通过 `--reference-map` 提供从产物 ID 到路径的 JSON 映射，Python 参数为 `reference_paths`。血缘验证保留摘要来源。有匹配的本地 Run 事件时，记录阶段时间。归档图使用包内证据。


## 资源预检和大输入

在原预测命令追加 `--dry-run`，使用同一输入、模型目录与输出格式，可查看 JSON 资源报告。该命令流式检查 FASTA、FASTQ 和 gzip，报告记录数、长度分布、输入摘要、模型文件、预计算特征占用和各卷空间。它不建 Run、输出目录或临时记录，也不启动模型进程。精确查重会在内存中保存首个 header 标记。正式预测使用临时磁盘索引查重。

预测在创建大中间文件和加载模型前执行同一空间检查。各卷按并存阶段累计必需字节，输出、编码检查点与临时目录若位于同一卷，只采样该卷的可用空间。确定空间不足时错误给出阶段、卷和所需／可用字节。XLSX 的临时 XML 与压缩尺寸为估算值，内存为系统取样，可用空间会随运行变化。缺少可选的 psutil 时，内存取样显示为 null，磁盘检查仍可执行。

编码按原长度分桶和原批次成员读临时索引，分类按原行号写分块数组及表格。原始输入与预计算特征保持只读。精确匹配 IF-009 至 IF-011 已发布编码器身份的已提交批次可以续算。输入、模型、参数或环境身份不符时会拒绝复用。显式允许超范围时，摘要仍保留相应原始行号。

## 轻量输出和独立 XLSX 报告

默认预测交付原有四个 CSV、NPZ 和 XLSX。需要 CSV/NPZ 时，在原预测命令加 `--output-formats csv,npz`，或给 `predict()` 传入 `output_formats="csv,npz"`。此模式交付五个文件及各自旁车，跳过报告进程。新结果的 `run_summary.json` 在 `delivery` 中记录声明格式、已产出的格式、计算内容摘要和报告状态。

请求的 XLSX 失败时，CLI 退出码为 2，并打印 Run ID 和已发布的 CSV/NPZ 目录。Python `IncFinderError.computed_output` 给出该目录。报告期间强制终止后，可使用 `incfinder.reconcile_run(run_id)` 核验并恢复预先封存的计算结果。请先确保原进程已退出。已有目标目录受保护，存在冲突时须先解决，才可重试恢复。

从已封存的结果目录或归档生成一份独立报告，例如 `incfinder report results/prediction --output results/report`。Python 对应 `rebuild_report("results/prediction", "results/report")`。可用 `--manifest-sha256` 或 `expected_manifest_sha256` 传入另行保存的来源清单摘要。重建核验封存的文件及已有旁车，读取保留的预测元数据与 CSV。引用归档即使原始输入、特征或模型不可用，仍可重建报告。

报告写入新的目录，包含 XLSX、对应 JSON 旁车、报告环境和 `report_receipt.json`。收据记录来源 Run ID、清单摘要、输入表和派生关系。需要重试时，继续用同一计算结果并选择空闲目标。工作簿内容按工作表、列序、行号、单元格值与类型核对，文件摘要用于核验交付的具体副本。

## doctor / check

```bash
incfinder doctor --model-dir models
incfinder check --model-dir models
incfinder check --quick --model-dir models
```

`doctor` 报告实际依赖版本、资源位置、模型定位来源、未加载模型和未探测 GPU 的状态。`check` 默认完成模型 SHA-256；`--quick` 的状态明确为 `SIZE_CHECK_ONLY`。

## Python 与旧接口

```python
from incfinder import predict
summary = predict(input="examples/sample.fasta", output="results/api", model_dir="models", allow_encoder_drift=True)
```

Python 参数与 CLI 对应，使用下划线；`predict()` 返回摘要并以 `IncFinderError` 表达操作错误。`python -m incfinder` 与控制台命令相同。旧 `python run.py predict --input examples/sample.fasta --output results/legacy --allow-encoder-drift` 仍按软件根目录解析相对路径。

## Docker 包装

```bash
bash docker/run.sh predict examples/sample.fastq -o results/container --model-dir models --allow-encoder-drift
bash docker/run.sh help predict
```

包装脚本保留输入文件的 gzip 扩展名；仅在 Docker 进程设置 MSYS 转换选项。挂载路径含逗号时给出明确错误，因为 Docker 的 mount 参数使用逗号分隔。

## V2 输出口径

最终 21 类 Inc 全零时对外输出 Inc Unclassified；内部保留 Unclassified、Inc_no_call、Inc_called。Stage1_pred 仅表示门控，最终标签表和结果表补充 gate_pass/internal_status。ct 与 u05 分别沿用原冻结阈值和统一 0.5，不根据本轮结果选阈值。[完整字段与迁移说明](output_policy.md)。
