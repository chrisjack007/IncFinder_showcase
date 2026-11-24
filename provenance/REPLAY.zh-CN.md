# 发行核验与分阶段复算

[English](REPLAY.md) · [分型溯源指南](README.zh-CN.md) · [数据 Release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2-provenance.1)

## 下载、验签与检查

在新的空目录运行以下 Bash 命令。核验程序使用 Python 3.10 或更新版本的标准库。签名公钥固定到原软件标签。发行清单分别列出冻结数据、复算数据、比对结果、复算输入索引和工具包。

```bash
base=https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-provenance.1
prefix=IncFinder-0.5.0rc2-provenance.1
for section in frozen replay controls comparison tools; do
  curl -fL --retry 3 -O "$base/$prefix-$section.tar.gz"
done
for file in PROVENANCE-SHA256SUMS PROVENANCE-SHA256SUMS.sig incfinder_provenance.py; do
  curl -fL --retry 3 -O "$base/$file"
done
curl -fL --retry 3 -o allowed_signers https://raw.githubusercontent.com/chrisjack007/IncFinder_showcase/v0.5.0rc2/licenses/incfinder-release-allowed-signers
ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s PROVENANCE-SHA256SUMS.sig < PROVENANCE-SHA256SUMS &&
  sha256sum --check PROVENANCE-SHA256SUMS
```

签名和文件摘要都通过后，解压并检查两份数据。核验会从逐行数据重算每个簇的代表、各亚型和最终标签的覆盖数，检查全部片段的来源、坐标、标签、折号及训练与留出质粒是否交叉。已下载附件也可运行 `python incfinder_provenance.py verify-release . --signer allowed_signers`。

```bash
prefix=IncFinder-0.5.0rc2-provenance.1
for section in frozen replay controls comparison tools; do
  tar -xzf "$prefix-$section.tar.gz"
done
python incfinder_provenance.py verify --data frozen
python incfinder_provenance.py verify --data replay
python incfinder_provenance.py verify-edges --data frozen \
  --rows comparison/frozen-directed-rows.tsv.gz --index comparison/frozen-skani-edges.tsv.gz
```

## 本次全量复算与差异

| 阶段 | 冻结结果 | 本次全量复算 | 逐行差异 |
|---|---:|---:|---|
| GenBank 提取 | 97,904 条质粒 | 97,904 条 | 版本化 accession、长度及序列 SHA-256 全部一致。 |
| skani 搜索 | 3,544,354 条合格有向行 | 3,544,354 条 | 双端有差异的查询 0 个。双端或 ANI、AF 有差异的查询 0 个。 |
| skani 聚类 | 45,911 个簇 | 45,911 个 | 所在簇成员集合发生变化的质粒 0 条，代表发生变化的质粒 0 条。 |
| MOB-suite 完整代表分型 | 45,911 条代表 | 45,911 条 | 对历史合并报告，原始复制子文本、Inc 亚型集合、最终标签有差异的代表依次为 0、0、0 条。对 rc2 分型表，亚型、最终标签的差异依次为 0、0 条。 |
| 亚型归并 | 39 个亚型、21 个阳性类别 | 39 个亚型、21 个阳性类别 | 映射、理由或支持数有变化的亚型 0 行。最终类别汇总有变化 0 行。 |
| MetaSim 及短质粒补入 | 800,265 条训练、200,095 条留出 | 800,265 条训练、200,095 条留出 | 训练 0 条、留出 0 条记录存在字段差异。 |

两套数据均保留 19 个历史片段元数据字段。将原机器上的输出路径统一到批次、种子与文件名后，训练和留出的 19 字段表分别有 0 条和 0 条记录不同。360 条补入的完整短质粒没有抽样输出路径。

本次从新恢复的 GenBank 序列重建两份代表 FASTA 和全部 28 份有序批次输入，校验其 SHA-256，再运行有许可的 MetaSim。全部批次另在冻结阶段输入上独立运行。GX2 作业对 479 个 MOB-suite 批次及 196 个 skani 搜索分片分别保留回执。比较包保留每条合格边的双端、ANI 与双向 AF，也保留有差异的查询、代表、片段及字段原值。文件中路径的工作目录会随运行环境改变，比较使用版本化 accession 和原报告输出的数值。

各阶段的逐行比较均一致，没有首个分歧阶段。

两次独立导出的有向比对表解压后，SHA-256 均为 `533fd7cfaf52e2d0f0adc1c238681a32bf269eb720d02455ea579a8d7204c284`。比较包保存一份完整的相同比对表，以及两次运行各自的查询索引。

另外按记录的坐标和方向，从重新恢复的来源质粒重建了 1,000,360 条模型输入序列。每条 SHA-256 均与冻结记录相符，其中 155,924 条由多个区段拼接。分划分的回执见比较包中的 source-coordinate-checks.json。

历史 MOB-suite 合并报告中的复制子原值保存在 frozen/representatives.tsv.gz。controls/rc2-typing.tsv.gz 是 rc2 数据准备实际采用的三列分型表。replay/representatives.tsv.gz 保存本次原版 mob_typer 的复制子原值。历史对账报告记载，整理工作表的 rep_type_mismatch 为 17,929，Inc 亚型与最终标签的差异均为 0。本次也从工作表全部 45,911 条记录重新提取了 Inc 亚型。上表的原始文本比较使用历史合并报告和本次原版程序的输出。

## 如何取得原始输入

controls/source_records.tsv.gz 列出原始质粒的版本化组装号、序列号、序列摘要和压缩成员身份。controls/archive_members.tsv.gz 覆盖快照中全部 79,940 个 genomic.gbff.gz 成员。controls/catalog.tsv.gz 保存日期目录的组装号及发布日期。可用查询结果中的 NCBI 链接查看单条记录，也可按组装号获取 GenBank 格式的上游数据。当前下载包的压缩封装或注释版本可能与历史快照不同，请核对成员和序列摘要。[NCBI Datasets 下载说明](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/download-genome/)

原始 tar 共 221,691,648,000 字节。历史包清单中 GCA_022558465.1 的压缩成员曾用官方 gzip 来源另行核实，成员表保留原 MD5、实际 MD5 及核验依据。若持有解开的 GBFF 成员，可把按 archive_member 相对路径组织的目录传给 genbank，使用 `--member-index controls/archive_members.tsv.gz`。若仅压缩封装不同，可显式添加 `--allow-repacked-members`。程序记录每个变化的容器，仍要求每条质粒的 accession、长度与序列 SHA-256 一致。这种运行只证明提取的记录等价。

如需从 NCBI 重新获取源文件，可在发行附件的解压目录用 NCBI Datasets CLI 读取带版本的组装清单。公共服务提供这些版本当前可下载的 GBFF。若有已撤下的组装、影响质粒提取的注释变化或缺失的成员，检查会报告对应记录。本次全量复算使用历史 tar，目录模式另外用原始、重新压缩和序列变化的记录测过。[NCBI 大批量下载说明](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/how-tos/genomes/large-download/) · [NCBI 组装包字段](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/reference-docs/data-packages/genome/)

```bash
gzip -cd controls/archive_members.tsv.gz | awk -F '\t' 'NR > 1 {print $1}' > snapshot-accessions.txt
datasets download genome accession --inputfile snapshot-accessions.txt --include gbff --dehydrated --filename source1.zip
unzip -q source1.zip -d source1_bacteria_genbank_complete
datasets rehydrate --directory source1_bacteria_genbank_complete
find source1_bacteria_genbank_complete/ncbi_dataset/data -type f -name genomic.gbff -exec gzip -nk {} +
python incfinder_provenance.py replay genbank . controls/source_records.tsv.gz work/restored-shards \
  --member-index controls/archive_members.tsv.gz --allow-repacked-members
```

MOB-suite 使用 3.1.9。其历史全量作业日志保存了软件版本，未保存当时 44 件参考文件的整体内容摘要。本次使用 rc2 核验的数据库，controls/mob-database.json 列出每件文件的大小和 SHA-256。实际复制子参考 rep.dna.fas 与 DOI 版本一致。完整数据库身份和该单件参考的身份分别核验。[MOB-suite](https://github.com/phac-nml/mob-suite) · [数据库发布记录](https://doi.org/10.5281/zenodo.10304948)

MetaSim 使用获许可安装的 0.9.5。数据包保存种子、抽样分箱、各批次输入顺序、原始输出摘要和软件环境摘要。原日志中的可执行文件摘要与 Java 版本标为未记录。本次 MetaSim 安装程序保留在原运行环境。[MetaSim 原始论文](https://doi.org/10.1371/journal.pone.0003373)

## 分阶段运行

下面的 Linux 命令假定 SNAPSHOT 是本地原始 tar 的路径，SKANI、MOB_TYPER、METASIM 是已安装程序的绝对路径，MOB_DB 是接下来设置的参考库目录。使用独立的 work 目录。WSL2 可执行相同的 MetaSim 命令。

```bash
python incfinder_provenance.py replay genbank "$SNAPSHOT" controls/source_records.tsv.gz work/shards \
  --archive-sha256 aa88e7242d488326ce2900c3bb289f2ae290e2fee227504c21c622c1c01d3845
python incfinder_provenance.py replay skani --program "$SKANI" --shards work/shards \
  --catalog controls/catalog.tsv.gz --dated controls/dated.tsv.gz --inventory controls/source_records.tsv.gz \
  --membership controls/membership.tsv.gz --output work/skani --threads 32 --profile controls/skani-profile.json
```

skani 用全部已映射记录初始化簇，搜索长度下限 500 bp，500 个查询为一批。建库和搜索采用 GX2 的 skani 0.3.1，Bioconda 构建号 ha6fb395_0。2026-04-22 的 Conda 安装记录和 2026-04-23 作业脚本中的环境名互相印证。原始标记文件与草图数据库给出的参数一致，压缩因子 125、k = 15、标记压缩因子 1,000、核酸模式、关闭 syncmer。历史索引包含全部 97,750 条至少 500 bp 的记录，其余 148 条已知日期的质粒仍保留为图节点。恢复的查询顺序与日期表一致。[skani 0.3.1](https://github.com/bluenote-1577/skani/releases/tag/v0.3.1)

读者可在独立目录用 MOB-suite 3.1.9 的 mob_init 初始化公开参考库。此版本使用 2023-12-08 发布的 Zenodo 数据库 v. 3.1.8。本次另行读取 DOI 归档的全部 472,851,578 字节，其 SHA-256 为 531f2124269b13e6fe59d0b01936276b5a47c0c0eebbc787f710cd376f77d3cd。8 个原始数据文件逐件与 rc2 快照相符。下面的命令核对这 8 件，记录本机全部文件的实际摘要，并逐条对照分型。若持有本次 GX2 所用的 44 件完整快照，可将 MOB_DB 指向其目录，省略初始化和 --upstream-reference 参数，执行严格的整库核验。新初始化的索引、运行标记与分类缓存可能有不同的摘要。在独立目录中，用已核验的 DOI 缓存和一份有记录的 NCBI 分类快照实际运行原版初始化器，得到 43 件文件。4 条对照代表的原始复制子、亚型和最终标签与 GX2 相同。完整 45,911 条的复算使用 GX2 的 44 件快照。两种目录的实际差异见比较包中的 initialized-reference-control.json。Zenodo 的数据库记录署名 Robertson，使用 CC BY 4.0 许可。 [Zenodo](https://doi.org/10.5281/zenodo.10304948) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) · [MOB-suite 3.1.9](https://github.com/phac-nml/mob-suite/blob/3.1.9/mob_suite/config.json)

```bash
export PATH="$(dirname "$MOB_TYPER"):$PATH"
MOB_DB="$PWD/work/mob-db"
mkdir -p work/mob-init
(cd work/mob-init && mob_init -d "$MOB_DB")
```

```bash
python incfinder_provenance.py replay inputs controls work/inputs --fasta-dir work/skani/fasta
python incfinder_provenance.py replay mob \
  --fasta work/inputs/00_input/train.plasmids.ANI80_AF90.fa work/inputs/00_input/test.plasmids.ANI80_AF90.fa \
  --program "$MOB_TYPER" --database "$MOB_DB" --database-manifest controls/mob-database.json \
  --upstream-reference controls/mob-upstream-reference.json \
  --policy controls/category_policy.tsv --reference controls/rc2-typing.tsv.gz --output work/mob
python incfinder_provenance.py replay rules work/mob/typing.tsv work/recomputed-policy.tsv
```

inputs 按冻结顺序重建两份代表 FASTA 和 28 份批次 FASTA，并校验 30 个文件的摘要。每批输入保留去重后参与抽样的真实顺序。训练、留出另有 221 和 34 个同序列别名，可在 duplicate_sequence_aliases 表追溯。mob 根据所选参考来源核对文件，再用原版 mob_typer 的逐质粒模式执行分型。多条输入共用一次调用前，以单条调用核对复制子原值。rules 从实际原始字段重新计数并执行 2026-05-07 的归并规则。

有两份重建的代表 FASTA 时，可直接按每条记录的坐标和方向取段。程序按原顺序拼接跨环区段、核对全长短质粒，并重新计算每一条序列的 SHA-256。

```bash
python incfinder_provenance.py verify --data frozen --representative-inputs work/inputs
```

```bash
for split in train test; do
  python incfinder_provenance.py replay metasim work/inputs work/metasim --metasim "$METASIM" --split "$split" || exit 1
done
python incfinder_provenance.py replay assemble work/inputs work/metasim work/final
python incfinder_provenance.py replay reconcile \
  --frozen frozen --controls controls --genbank work/shards --skani work/skani \
  --mob work/mob --assembled work/final --output work/replay --comparison work/comparison
python incfinder_provenance.py verify --data work/replay
```

assemble 按批次和读段顺序拼接，按代表 FASTA 的原顺序补入小于 1,000 bp 的完整质粒，再规范化序列。reconcile 从复算的原始复制子字段重新计算归并规则，连接本次聚类与分型结果，再从新生成的 FASTA 逐条核对来源、区段、方向、长度、序列摘要、划分、折号及继承标签。独立复算时先用固定阶段输入定位差异。若新代表集合发生变化，应同时保存新集合上的下游结果和冻结输入上的阶段对照。
