# IncFinder 0.5.0rc2 分发证据 / Distribution evidence

推理软件版本为 `0.5.0rc2`。运行 `v0.5.0rc2-dist.1` 后，可用平台参考核验官方示例。完整发行签名清单覆盖证据 ZIP。

| 证据包中的位置 | 内容 |
| --- | --- |
| `science/MODEL_CARD.zh-CN.md`、`science/MODEL_CARD.en.md` | 模型适用范围、训练与留出验证 |
| `science/LICENSE`、`science/MODEL-LICENSE.md` | 软件与模型许可 |
| `science/holdout_metrics.json`、`science/per_class_metrics.tsv` | 总体与逐类性能 |
| `science/example-reference.json` | 官方示例的分平台数值参考 |
| `docs/SBOM-*.cdx.json`、`science/SBOM-*.cdx.json` | 便携平台与原始科学包的软件组成 |
| `science/public-source-tree.json`、`science/release-manifest.json` | 公开源码与科学发行身份 |
| `rf-index.json`、`rf-index.json.sig` | 随包 RF 来源、分片及重组模型摘要和签名 |
| `provenance.json` | 大体量复算附件的固定标签、文件名与 SHA-256 |

两条官方记录的通过结论只适用于该次示例。原始序列运行沿用 `RESEARCH_PREVIEW / NOT_ESTABLISHED`。[科研溯源材料](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2-provenance.1)

[下载完整证据 ZIP](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-0.5.0rc2-dist.1-evidence.zip) · [中文模型卡](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/docs/MODEL_CARD.zh-CN.md) · [English model card](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/docs/MODEL_CARD.en.md) · [总体指标](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/docs/holdout_metrics.json) · [逐类指标](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/docs/per_class_metrics.tsv) · [官方示例参考](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/NT_V3_650M_TSRF_SW/examples/expected_outputs/raw_cpu.json) · [溯源附件摘要](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2-dist.1/distribution/provenance.json)
