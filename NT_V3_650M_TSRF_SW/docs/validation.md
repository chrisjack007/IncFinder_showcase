# Engineering validation / 工程验收

The matrix below is the historical engineering validation. See the [0.5.0rc2 model card](MODEL_CARD.en.md) for the independent RF holdout evaluation.

## Post-release platform and CUDA checks

The [validation matrix](validation_matrix.json) records 32 platform, device, input and threshold combinations for baseline `f743f03`. The installed runtime completed 40 predictions on the bundled 11 records and the locked IF-004 82-record research cohort. Each result records the environment and references used. Values below follow `ct` then `u05` when the modes differ.

| Context | 11 raw features vs V2 | 82 raw features vs IF-004 CPU | 11 frozen RF | 82 frozen RF | 11 raw decisions | 82 raw decisions |
| --- | --- | --- | --- | --- | --- | --- |
| Windows native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Windows native RTX 5070 Ti | FAIL | FAIL | NOT_TESTED | NOT_TESTED | PASS | PASS |
| Linux native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Linux native Tesla V100 | FAIL | PASS | NOT_TESTED | NOT_TESTED | PASS | PASS |
| WSL2 Ubuntu CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Docker CPU | FAIL | PASS | PASS | PASS | PASS | PASS |

Raw comparisons require finite float32 arrays, matched record order and the existing tolerance `atol=1e-4`, `rtol=1e-5`. Frozen RF compares both probability arrays exactly, including the Stage 2 NA mask. Raw decisions compare gates, masks and all 21 calls against the same platform's frozen predictions. The matrix records probability drift separately. CUDA applies to native raw encoding. WSL2 and the repository Docker image use CPU. The tested Windows bind mount returned EINVAL for atomic no-replace output publication. Two sealed Docker computations were reconciled on the container's POSIX filesystem with their bytes verified. The Docker matrix uses that filesystem for output publication. The results retain `RESEARCH_PREVIEW`.

## V2 release acceptance

**NT_V3_650M_TSRF_SW_V2 · software 0.4.0 · Conda py_0 · output schema 2.** Validated 2026-11-17 (Asia/Shanghai).

Engineering acceptance passed. Scientific release status remains **RESEARCH_PREVIEW**; new-sequence equivalence remains **NOT_ESTABLISHED**.

| Check | Result and measured scope |
|---|---|
| Unit tests | 53/53 pass from source and with the installed Windows package |
| Frozen assets | All 14 original SHA-256 values match; 25,904,505,766 bytes |
| Original scientific fixtures | All five files / 102,462 bytes remain unchanged |
| Runtime packages | Wheel and Conda have 27 identical code/resource files; final source matches them |
| Windows install | Independent Python 3.12 environment created from win-64.lock; actual rebuilt package installed |
| WSL install | Existing isolated Linux lock environment upgraded to the rebuilt package |
| Docker install | Actual image build with fixed official base digest, Linux lock and the same Conda package |
| Frozen ct and u05 | Both modes pass in Windows, WSL and Docker: original probabilities, NaN masks, gates and calls are exact |
| Public and native decisions | ct: external 6/5, native 5/1/5; u05: external 8/3, native 5/3/3 |
| Cross-platform outputs | All seven NPZ arrays, four CSV tables and XLSX cell contents agree within each mode |
| Windows formats | FASTA, repeat FASTA, FASTQ, FASTA.gz and FASTQ.gz pass; RF arrays/states/tables are exact across formats |
| Python API | Same arrays/tables as CLI with the same supplied features; returned summary matches JSON; host CWD/environment preserved |
| Failure guards | Preview opt-in, overwrite refusal with unchanged existing files, invalid model and invalid sequence checks pass |
| English code | First-party source and executable documentation examples pass the Chinese-character scan; Unicode path tests remain |

External counts above are Unclassified/Classified. Native counts are rejected/no-call/called. External Unclassified is exactly final 21-label zero; prediction does not establish ground truth. See [output policy](output_policy.md) and [frozen result examples](../examples/expected_outputs/README.md).

## Numerical and resource limits

V2 Windows raw encoding still fails the original frozen-reference tolerance: maximum absolute difference **0.1968994140625**, versus unchanged atol=1e-4 / rtol=1e-5. Raw prediction requires --allow-encoder-drift. Matching calls on 11 records does not establish encoding equivalence or accuracy.

Same-input/format FP32 features varied by up to **6.103515625e-05**, within the original tolerance. Bitwise feature equality was false; RF probabilities, calls, native states and tables remained exact across the tested formats. Feature bitwise reproducibility is not promised.

The historical V1 GPU difference was 0.0301513671875 and also failed tolerance. V2 performed no new GPU validation. Its WSL/Docker acceptance covers frozen-feature RF deployment, not new raw-sequence encoding on Linux.

Windows raw runs took approximately 74–76 seconds for these 11 records. Maximum sampled process-tree RSS was **42.718 GiB**; parent/child RSS can double-count shared pages. This is not a minimum-memory requirement or a comparative throughput benchmark.

The selected set contains 11 sequences, 28,087 bases, lengths 212–10,000 bp, and (11,1536) float32 features. No training, threshold reselection or performance metric estimation occurred.

## Package identity

- Conda SHA-256: 0b1469d5d167028b534f9c08258724107c09f668987dd1674a8085e0bd3f4e7d
- Wheel SHA-256: ac20d6bce076357403e65f60c7f14c2b50a76f9bd7b5b5777a101236f6dff0d9
- Installed software-manifest SHA-256: 2bcb0cfe68f9e0677787e93e818a53f1bc194bd704794e7e5103394fea8b9cf6

Release SHA256SUMS covers the downloadable attachments. MODEL_ASSET_REFERENCES.json links unchanged RF bytes to V1 and the pinned official NTv3 revision. Original asset identities remain authoritative in config/assets.json.

## 中文说明

53 项测试通过。三端 ct/u05 冻结推理的概率、NA 掩码、门控、21 类调用和新增状态均符合参考，外部未分类分别为 6 条和 8 条；CSV、XLSX、NPZ、JSON 与 API 已核对。原模型、阈值、科学合同和五个冻结测试载荷不变。

Windows 四种输入格式、重复运行、API 及失败保护已实测。原编码严格容差仍失败；同输入特征也存在容差内差异。继续保留研究预览状态，不宣称原编码等价、逐值特征可重复、总体准确率或宏基因组性能。
