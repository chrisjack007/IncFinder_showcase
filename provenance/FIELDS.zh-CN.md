# 字段与计数口径

数据包使用 UTF-8、制表符和表头。每个表都单独 gzip 压缩。空字段表示不适用或原始字段为空。SHA-256 用小写十六进制表示。冻结与复算视图使用相同的字段名。

| 文件 | 一行代表什么 | 连接键和字段含义 |
|---|---|---|
| accessions.tsv.gz | 原始快照中的一条质粒记录 | assembly_accession 与 sequence_accession 组成唯一键，二者均带版本号。length_bp、sequence_sha256 描述大写原始序列，gbff_sha256 描述所在的压缩 GenBank 成员。assembly_release_date 来自冻结组装目录。inclusion 记录是否进入日期映射。 |
| clusters.tsv.gz | 一条已取得日期的质粒 | 用复合键连接 accessions。cluster_id、cluster_size 是本次视图的簇号、簇大小。representative_assembly、representative_accession 指向代表，is_representative 取 0 或 1。不同运行间用完整成员集合比较簇。 |
| representatives.tsv.gz | 一条完整代表质粒 | record_id 是版本化序列 accession。raw_rep_types 保留本视图 MOB-suite 报告的复制子字段。inc_subtypes 是据此提取并去重的原始 Inc 亚型。final_labels 按 rc2 列顺序列出最终类别。split 为 train 或 holdout，cv_fold 是训练质粒的 1–10 折。 |
| subtypes.tsv.gz | 一条历史归并规则 | raw_subtype、major_group、final_label 和 rule 指明每个原始亚型的归属与理由。major_plasmids 和 subtype_plasmids 是在定义类别的全体代表上分别去重计得的数量。两个计数不能直接相加求归并后的覆盖数。 |
| train_records.tsv.gz、holdout_records.tsv.gz | 一条进入模型的数据记录 | record_id 是训练或留出记录 ID，row_index 从 0 开始，分别在各自划分内编号。parent_accession、assembly_accession 连接代表和原始记录。final_labels、cv_fold 继承自来源质粒。length_bp、sequence_sha256 对应实际进入模型的规范化序列。 |
| labels.tsv.gz | 一个最终类别，另列 No_Inc | stage2_index 是第二层 21 维概率向量中从 0 开始的列号。No_Inc 此列为空。representative_plasmids 是带该标签的不同代表数。training_records、holdout_records 是带该标签的各划分记录数。raw_subtypes 列出参与归并的成员。 |
| sources.json、manifest.json | 一个视图 | 前者记录输入身份或运行来源，后者记录每个附件的字节数和 SHA-256。数据 Release 的签名清单绑定公开发行附件。 |

record_type 为 metasim_fragment_1000_10000 时，source_batch_id 指向实际批次，source_segments 按拼接顺序保存方向和区间。坐标从 0 开始、右端不含在内。fw 表示按正链取段，bw 表示取反向互补后拼接。跨环起点时保留多段。source_strands 是该记录出现过的方向。历史类型名中的 1000 至 10000 表示模拟目标，实际产物可短于 1000 bp。

record_type 为 full_plasmid_lt1000 时，记录覆盖整条来源质粒。这些记录的批次、区段和方向字段为空。来源 GenBank 序列可能含有模糊碱基，模型输入把 A、C、G、T、N 之外的字符规范化为 N。比对原始序列摘要和模型输入摘要时应采用相应口径。

inclusion 为 assembly_absent_from_frozen_date_catalog 的行保留在原始清单中，日期字段为空，没有下游簇或训练记录。No_Inc 表示按本研究的 MOB-suite 复制子规则未得到 Inc 亚型，适用于第一层标签。原始复制子字段的顺序、重复和非 Inc 名称都保留在 raw_rep_types 中。

同一质粒可以具有多个最终标签。一条记录在同一标签内只计一次，因此各类别计数之和可以超过记录总数。训练与留出以质粒为分组单位，组间无交叉。模型卡另列两侧相同序列的检查结果。


反查完整短质粒时，查询工具会给出从 0 到来源质粒全长的区间，方向为原序列正向。抽样片段则按拼接顺序给出原记录的各段。两者都采用从 0 开始、右端不包含的坐标。

## 原始片段表的 19 个字段

train_source_metadata.tsv.gz 和 holdout_source_metadata.tsv.gz 逐行保留历史元数据。通过 record_id 连接相应的模型记录表。

| 历史字段 | 含义与空值 |
|---|---|
| `record_id`, `record_type`, `split` | 模型记录 ID、记录的生成方式与历史划分名 train 或 test。test 对应模型表的 holdout。 |
| `plasmid_id`, `original_header`, `original_length_bp` | 来源质粒的版本化序列 accession、代表 FASTA 的完整原始记录头、来源长度（bp）。 |
| `sequence_length`, `n_count`, `ambiguous_replaced_count` | 模型输入长度（bp）、最终所有 N 的数量、其中由其他模糊字符转换为 N 的数量。 |
| `source_status` | metasim_sampled_ge1000_plasmid 表示由至少 1,000 bp 的亲本抽样。not_sampled_lt1000_plasmid_included_full_length 表示补入不足 1,000 bp 的完整质粒。 |
| `source_gi`, `source_ref`, `metasim_read_id` | 模拟器输入中使用的 GI 和参考序号，以及批次内的读段 ID。完整短质粒留空。 |
| `raw_metasim_header` | 原始模拟器读段的完整记录头。完整短质粒留空。 |
| `source_batch_id`, `source_fna` | 抽样批次 ID 及模拟输出文件。原机器上的绝对路径在两套表中都转为含批次、种子和文件名的相对路径。完整短质粒留空。 |
| `source_segments`, `source_strands` | 依拼接顺序记录每段的方向和区间，以及该读段出现的方向集合。坐标从 0 起，右端不包含在区段内。补入的完整质粒留空。 |
| `original_sequence_sha256` | 仅完整短质粒填写。该摘要对应字符规范化后的全长序列，与模型表中相应记录的 sequence_sha256 一致。 |

## 搜索与抽样证据

比较包中的 frozen-directed-rows.tsv.gz 每行保留一个合格的有向比对，查询端和参考端分别用竖线连接版本化组装与序列 accession。其余三列分别是原报告的 ANI、参考端 AF 和查询端 AF，单位均为百分比。逐查询摘要保留重复行和三个原始指标。没有合格非自比行的查询不出现在摘要表。复算边表与摘要的对应关系见比较包内的边差异报告。verify-edges 可用全部边和孤立节点重新构造连通分量。

metasim-batches.tsv 给出每批的划分、种子、记录数、按原顺序构建的输入摘要、历史输出摘要和两次独立运行的输出摘要。metasim-environment.json 列出本次实际使用的启动程序、JAR 和 Java 的身份。typing-differences.tsv.gz 逐条比较原合并报告、rc2 采用的亚型与标签和新的分型结果。差异文件只有表头表示该项比较未发现差异记录。
