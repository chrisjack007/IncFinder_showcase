# 软件包中的表格怎样读

[English](PACKAGE-TABLES.en.md) · [中文首页](../README.zh-CN.md)

这里按原软件包的版本和测试集解读安装、输出与验证表。rc2 在独立留出集上的成绩见模型卡的[整体性能](MODEL_CARD.zh-CN.md#aggregate)和[各类别结果](MODEL_CARD.zh-CN.md#per-label)。两种语言的发行版模型卡均对应这两张表。[包内中文原文](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/MODEL_CARD.zh-CN.md) · [包内英文原文](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/MODEL_CARD.en.md)

<a id="install"></a>

## 安装命令

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/README.zh-CN.md?plain=1#L5) · [同表英文原文](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/README.md?plain=1#L5)

每行选一种原版 rc2 环境。左列是平台，右列是在解压后的软件包根目录运行的命令。Windows 和 Linux 使用各自的锁文件创建 Conda 环境，WSL2 与原生 Linux 共用 Linux 锁文件，Docker 行构建 Linux 镜像。

| 平台 | 环境准备命令 |
|---|---|
| Windows x86-64 | `conda create -n IncFinder_Test1 --file environments/win-64.lock` |
| WSL2 / Linux x86-64 | `conda create -n IncFinder_Test1 --file environments/linux-64.lock` |
| Docker (Linux x86-64) | `docker build -t incfinder-ntv3-tsrf:0.5.0rc2 .` |

<a id="options"></a>

## 预测参数

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/usage.md?plain=1#L44)

每行是 `incfinder predict` 的一个输入或选项。左列保留命令行拼写，右列说明用途，有默认值时一并给出。`--threads` 数编码线程，`--jobs` 数随机森林任务，`--batch-size` 数每批记录，三者控制不同环节。

| 参数 | 含义与默认值 |
|---|---|
| 位置输入或 `-i / --i / --input` | 输入一个文件，可用 gzip 压缩。位置参数和具名参数二选一。 |
| `-o / --o / --output` | 选择尚不存在的结果目录。 |
| `--model-dir` | 包含 `ntv3/` 和 `rf/` 的模型目录。 |
| `--device` | 默认 `cpu`。安装相应 GPU 依赖后可显式选择 `cuda`。原参数表记录的是 CPU 验证。 |
| `--threads` | 编码使用的线程数，默认 4。 |
| `--jobs` | 随机森林的任务数，默认 1。 |
| `--batch-size` | 每批送入编码器的记录数，默认 1。 |
| `--threshold-mode` | 默认 `ct`，也可选择 `u05`。使用发行版阈值。 |
| `--features-dir` | 已有 `features.npy` 和配套的 `features.json` 的目录。输入 ID、顺序、格式、序列摘要和模型合同均须匹配。 |
| `--allow-encoder-drift` | 新序列研究预览需要显式接受原始序列编码的等价性尚未确立。 |
| `--max-length` | 长度上限，默认 10000 bp，超长记录不会被静默截断。 |
| `--allow-out-of-domain` | 显式接受超出训练长度范围的外推，该范围的效果仍待验证。 |
| `--parent-run-id` | 可选，关联同一记录目录中已结束的运行。 |

<a id="conditions"></a>

## V2 的输出状态

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/output_policy.md?plain=1#L7)

每行是一条记录可能走过的一种判定路径。左列给出门控和第二层条件，`internal_status` 保留内部状态。`Inc_unclassified`、`Inc_classified` 是互补的最终标志，1 表示该标志成立。`Result` 给出读者看到的文字或标签。门控未通过时，21 项第二层概率均缺失。门控通过但无标签达标时，有有效的第二层概率。

| 内部判定条件 | internal_status | Inc_unclassified | Inc_classified | Result |
|---|---|---|---|---|
| 第一层概率 < 0.5 | Unclassified | 1 | 0 | Inc Unclassified |
| 第一层概率 ≥ 0.5，无第二层标签达标 | Inc_no_call | 1 | 0 | Inc Unclassified |
| 第一层概率 ≥ 0.5，至少一个第二层标签达标 | Inc_called | 0 | 1 | 按分号分隔的标签 |

<a id="calls"></a>

## V2 的 11 条测试记录

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/output_policy.md?plain=1#L72)

每行是在同一组 11 条冻结记录上使用的一种阈值模式。`Rejected` 数第一层未通过的记录，`Gate-passing no-call` 数通过门控但无标签达标的记录，`Called` 数至少有一个标签的记录。这三列相加为 11。`External Unclassified` 是前两列之和，例如 `ct` 为 5 + 1 = 6。

| Mode | Rejected | Gate-passing no-call | Called | External Unclassified |
|---|---:|---:|---:|---:|
| ct | 5 | 1 | 5 | 6 |
| u05 | 5 | 3 | 3 | 8 |

<a id="platforms"></a>

## V2 的平台对照

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/validation.md?plain=1#L9)

每行对应一个操作系统与设备组合，列名中的 11 和 82 是两个测试集的记录数。前两项分别把重新编码的特征与 V2、IF-004 CPU 参考对照，要求记录顺序匹配、数值有限且满足 `atol=1e-4`、`rtol=1e-5`。两列 `frozen RF` 比较固定特征下两层概率和缺失位置是否完全相同。两列 `raw decisions` 将重新编码后的门控、缺失位置和 21 项判定与同平台冻结输入预测比较。

`PASS` 表示该项达到上述标准，`FAIL` 表示实际比较未达标，`NOT_TESTED` 表示这个环境未执行该项。沿一列可比较同一测试在不同平台的结果。这是软件 0.4.0、提交 `f743f03` 的工程验证。

| Context | 11 raw features vs V2 | 82 raw features vs IF-004 CPU | 11 frozen RF | 82 frozen RF | 11 raw decisions | 82 raw decisions |
| --- | --- | --- | --- | --- | --- | --- |
| Windows native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Windows native RTX 5070 Ti | FAIL | FAIL | NOT_TESTED | NOT_TESTED | PASS | PASS |
| Linux native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Linux native Tesla V100 | FAIL | PASS | NOT_TESTED | NOT_TESTED | PASS | PASS |
| WSL2 Ubuntu CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Docker CPU | FAIL | PASS | PASS | PASS | PASS | PASS |

<a id="checks"></a>

## V2 的发行验收

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/validation.md?plain=1#L26)

每行是一项软件 0.4.0（V2）验收。左列是检查对象，右列写实际结果和覆盖范围，单位随项目而变。判定计数中，对外的两个数依次为未分型、有分型，内部的三个数依次为门控拒绝、通过但无标签、有标签。两组数各自划分同一批记录。

| 检查项 | 结果与实测范围 |
|---|---|
| 单元测试 | 源码和 Windows 安装包的测试均为 53/53 通过。 |
| 冻结资源 | 14 项原始 SHA-256 全部匹配，合计 25,904,505,766 字节。 |
| 原始科研测试文件 | 5 个文件合计 102,462 字节，文件内容均与原件一致。 |
| 运行包 | Wheel 与 Conda 包中的 27 个代码及资源文件相同，也与最终源码一致。 |
| Windows 安装 | 从 `win-64.lock` 建立独立的 Python 3.12 环境，实际安装重建包。 |
| WSL 安装 | 在现有的独立 Linux 锁定环境中升级到重建包。 |
| Docker 安装 | 使用固定摘要的官方基础镜像、Linux 锁文件和同一 Conda 包实际构建镜像。 |
| 冻结输入下的 `ct`、`u05` | Windows、WSL、Docker 均通过。在每种模式下，原始概率、NaN 位置、门控及标签逐项相同。 |
| 对外结果与内部判定 | `ct` 对外 6/5，内部 5/1/5。`u05` 对外 8/3，内部 5/3/3。 |
| 跨平台输出 | 同一模式下，7 个 NPZ 数组、4 张 CSV 表和 Excel 单元格内容一致。 |
| Windows 输入格式 | FASTA、重复运行的 FASTA、FASTQ、FASTA.gz 和 FASTQ.gz 均通过。各格式的 RF 数组、状态和表格一致。 |
| Python 接口 | 提供相同特征时，API 与 CLI 输出相同的数组和表格，返回的摘要与 JSON 相符，调用端工作目录和环境保持原状。 |
| 异常输入与覆盖保护 | 研究预览确认、拒绝覆盖已有结果并保留原文件，以及无效模型和无效序列检查均通过。 |
| 英文代码 | 第一方源码和文档中的可执行示例通过中文字符扫描，Unicode 路径测试保留。 |

<a id="files"></a>

## 五个冻结测试文件

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/tests/fixtures/README.md?plain=1#L12)

每行标识一个冻结文件。左列是文件名，`Bytes` 是该文件的精确字节数，`SHA-256` 标识其完整字节内容。五个大小可以相加，合计 102,462 字节。校验时将每个文件的摘要与同行逐项比对。

| Payload | Bytes | SHA-256 |
|---|---:|---|
| expected.npz | 2204 | d0bbb31faf9b7a6fee8c13e9b271a0a75dc01bae08853580e5f0c792f68fb03d |
| features.json | 3572 | 7c55087a8cef6083a55c70fbf03dcb8020f5ee41f5afa4bdad6f6afdf478dbbc |
| features.npy | 67712 | 5f8bc5a7a46f6167c3e1e3db20808ed2572cfe84a786a32691879c43df69e96f |
| reference.fasta | 28357 | d77da8445d8ea189b9b64cc827e601ca6e29ba22401711a55b215ca97c29b336 |
| source_rows.json | 617 | 4f8f256dc34761d0b1cda442b23c06ca09d8da76ced2085ddc11a7d7fc7fcb13 |

<a id="arrays"></a>

## 11 条测试记录对应的数组

[发行包原表](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/tests/fixtures/README.md?plain=1#L34)

每行是一个数组。`Array` 指文件或 NPZ 成员，`Shape` 按维度顺序列出长度，`dtype` 是元素类型，最后一列解释元素的含义。首维 11 对应固定顺序的记录，第二维 1,536 是特征维数，21 是标签数。`bool` 表示真假，`uint8` 存放本表的 0/1 判定，`float32` 存放特征或概率。

| Array | Shape | dtype | 含义 |
|---|---|---|---|
| features.npy | (11,1536) | float32 | 行与 `reference.fasta` 的记录对应，列为编码维度。 |
| expected.npz:stage1 | (11,) | float32 | 各记录的第一层阳性概率。 |
| expected.npz:stage2 | (11,21) | float32 | 列顺序固定于 `contract.json`。未通过门控的记录整行是 NaN。 |
| expected.npz:gate | (11,) | bool | 第一层概率 ≥ 0.5 时为真。 |
| expected.npz:labels | (11,21) | uint8 | 冻结的 `ct` 阈值下，各标签是否达标（0/1）。 |
