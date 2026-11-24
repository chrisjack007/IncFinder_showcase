# 2026-05-07 的归并依据 / The 7 May 2026 classification decisions

[中文指南](README.zh-CN.md) · [English guide](README.md) · [39 行归并结果 / 39 subtype outcomes](subtype-policy.tsv)

历史说明和分类脚本都将这次人工修订注明为 2026-05-07。下表的中文逐行摘自历史说明中的分类规则，英文概述同一决策。

The archived explanation and classification script both date this revision to 7 May 2026. The Chinese columns reproduce its nine decision rows. The English column summarizes each decision.

| 历史条件 | 历史处理 | English summary |
|---|---|---|
| Inc 大类总 plasmid 数 < 100 | 整个大类合并为 `Inc_other` | Pool a major group with fewer than 100 distinct parents into Inc_other. |
| Inc 大类总 plasmid 数 >= 100，且只有 1 个子类型 | 使用大类名作为最终类别 | Use the major name when a group with at least 100 parents has one observed subtype. |
| Inc 大类总 plasmid 数 >= 100，且有多个子类型，某子类型 plasmid 数 >= 100 | 该子类型单独作为最终类别 | Retain a subtype supported by at least 100 parents within a multi-subtype major group. |
| Inc 大类总 plasmid 数 >= 100，且有多个子类型，某子类型 plasmid 数 < 100，但合并后的 `_other` 类别仍 >= 100 | 保留为可训练的 `<Inc大类>_other`，当前包括 `IncX_other` | Retain an eligible pooled low-count category when its support reaches 100. IncX_other is the retained example. |
| Inc 大类总 plasmid 数 >= 100，且有多个子类型，某子类型 plasmid 数 < 100，但对应 `_other` 类别 < 100 | 不再单独输出小 `_other`，并入大类或明确分支父类 | When the pooled category has insufficient support, assign its members to the major group or an explicit branch. |
| 明确同分支小类 | 先合并到分支父类，例如 `IncHI1A + IncHI1B -> IncHI1`、`IncHI2A -> IncHI2`、`IncI1 + IncI1/B/O -> IncI1`；如果该父类仍因 `<100` 规则触发大类合并，则继续并入大类 | Apply the explicit branch decisions first, including IncHI1A/B, IncHI2A and IncI1/B/O. The whole-major rule can still apply. |
| 无明确同分支归属的小类 | 并入大类，例如 `IncN2 -> IncN`、`IncQ2 -> IncQ`、`IncI2 -> IncI` | Assign a small subtype with no more specific branch destination to its major group, including IncN2, IncQ2 and IncI2. |
| 初步 final_label 数量仍 < 100 | 触发同一 `Inc+大写字母` 大类整体合并到大类标签；例如 `IncI-gamma/K1 + IncI1 + IncI2 + IncI1/B/O -> IncI`，`IncQ1 + IncQ2 -> IncQ` | If a preliminary category stays below 100, pool the eligible letter-named major group. This gave IncI and IncQ. |
| 未解析到任何 `rep=Inc*` | 归为 `No_Inc` | Assign No_Inc when the replicon field yields no Inc subtype. |

历史实现先在每个大类中相加归入同一初步标签的亚型计数，再判断是否触发整组合并。最终代表质粒数按不同的质粒 ID 去重。读者可以在签名的 controls 包中用 historical-policy-calculation.tsv 查看每个亚型的初步标签与计数。

The original code sums subtype support within each preliminary label before applying the whole-major rule. Final label coverage counts distinct parent plasmids. The signed controls archive includes the preliminary labels and counts for all 39 subtypes in historical-policy-calculation.tsv.

## 原始文件的身份 / Archived source identities

| 原件 / Archived file | 字节 / Bytes | SHA-256 |
|---|---:|---|
| MOB-suite_Inc分类规则_含NoInc_explanation.md | 18,099 | bdc46acbe887625e541d2af90b742460f67907228c8552283d1169b2091b7b56 |
| classify_mob_suite_inc_with_noinc.py | 23,229 | b1a36739e0777794e31a53aeee3b15957fc480f8a796fac1771ec1f9d909f9ec |
| MOB-suite_Inc分类规则_含NoInc.xlsx | 4,417,006 | 6fec74ddc0db96a772727d79b6d570418a5323688631409af3f68c65622dbb6d |

签名的 controls 包在 may07-policy-evidence.json 中保留了这些文件摘要和九条原始规则。复算代码可从逐质粒复制子字段重算归并表。[发行附件](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2-provenance.1) · [规则实现](replay/derive_rules.py)
