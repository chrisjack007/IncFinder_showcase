# Output schema 2: final Inc calls and native states

Software 0.4.0 / NT_V3_650M_TSRF_SW_V2 uses policy `external_unclassified_final21zero_v1`, defined in [output_contract.json](../config/output_contract.json).

External Unclassified is true exactly when all 21 final Inc calls are zero. A prediction is not ground truth: no call can reflect detection failure or an unsupported sequence and does not establish biological absence.

| Native condition | internal_status | Inc_unclassified | Inc_classified | Result |
|---|---|---:|---:|---|
| Stage1 < 0.5 | Unclassified | 1 | 0 | Inc Unclassified |
| Stage1 >= 0.5, no Stage2 call | Inc_no_call | 1 | 0 | Inc Unclassified |
| Stage1 >= 0.5, one or more calls | Inc_called | 0 | 1 | Semicolon-separated labels |

Both comparisons retain `>=`, including equality. Rejected rows always have zero calls and original Stage2 probabilities of NaN in NPZ / literal NA in CSV and XLSX. Invalid or missing predictions fail the run; they never become valid all-zero predictions.

## Files and fields

- `layer1_decisions.csv` / XLSX `Stage1`: `Stage1_pred` describes the native gate only. A gate-passing no-call still has Stage1_pred=Inc_classified.
- `layer2_probabilities.csv` / `Stage2_probability`: original 21 probabilities and gate flag.
- `inc_label_predictions.csv` / `Inc_prediction`: final external flags, threshold mode, 21 unchanged calls, and appended `gate_pass` / `internal_status`.
- `inc_results.csv` / `Inc_result`: labels or `Inc Unclassified`; `final_status` is `Inc_classified` or `Inc_unclassified`, with appended trace fields.
- All four tables retain `Scientific_status`; exact thresholds remain in `prediction.json`.

Original NPZ arrays are unchanged: stage1 float32 (N,), stage2 float32 (N,21), gate bool (N,), labels uint8 (N,21). Additions are external_unclassified bool (N,), external_classified bool (N,), and internal_status Unicode (N,). No object arrays or pickle loading are required.

`prediction.json.counts` holds two external counts; `internal_counts` holds the three mutually exclusive native counts; `gate_counts` holds passed/rejected counts. Each partition sums to N. The run summary records schema, policy ID and output-contract SHA-256. Python API parameters are unchanged and its summary contains these same results.

## Migration from V1

V1 final-table Inc_unclassified meant gate rejection and Inc_classified meant gate passage. V2 changes these fields to final-call semantics. V1 Inc_classified_no_call maps to internal Inc_no_call and external Inc_unclassified. V1 gate rejection maps to internal Unclassified; a called row maps to Inc_called. Consumers needing native rejection must use gate_pass, Stage1 fields or internal_status.

The V1 tag and artifacts remain available; V2 adds no legacy-output switch. Models, encoding contract, probabilities, label order, thresholds and numerical tolerances are unchanged.

`1 - Stage1_probability` scores native rejection and must not be presented as a continuous score for merged external Unclassified. This inference tool receives no truth labels and calculates no confusion matrix or AUC/AP. Downstream evaluation must recompute counts from final calls and distinguish binary AUC from scoring AUC.

## Version-aware metadata and explicit migration

Prediction and standalone classification validate the output contract before loading models or creating results. The validated version, policy ID and SHA-256 come from one read of the contract. The launcher checks the classifier's identity before reporting, then uses its validated contract for the summary and companions. V1 migration validates the same V2 target contract before copying or rewriting results.

The main prediction API/CLI publishes six companions: each of the four CSV files, `inc_predictions.xlsx` and `probabilities.npz` gains `<complete_filename>.metadata.json`. Sidecars are written and read back inside the temporary result directory before its final publication, and their hashes enter `run_summary.json.outputs`. Existing table fields, four-sheet layout and seven NPZ arrays are unchanged by this metadata addition. Frozen published examples remain unchanged.

Each companion uses `metadata_schema_version=1`, `output_schema_version=2` and the exact current policy ID. It records `result_file`, `result_sha256`, `output_contract_sha256`, `threshold_mode`, `stage1_threshold`, all 21 `stage2_thresholds`, `label_order`, `record_count` and `scientific_status`. Paths are filenames only; no new Run ID or absolute workstation path is inserted into these sidecars.

`load_output_metadata(result_path, *, source_version=None)` validates the companion's format, known policy, current contract identity, frozen label/threshold identities and actual file hash. It also checks version-bearing fields, CSV record counts and threshold modes, and NPZ names/shapes/dtypes without pickle loading. It returns a metadata dictionary; this guard does not replace complete numerical QC or establish an independently authenticated origin.

Without a sidecar, the caller must explicitly declare `source_version="v1"` or `"v2"` and keep matching prediction/run-summary JSON beside the result. The run-summary file hash must match, and missing identities are not guessed. Conflicting declarations cannot override a sidecar. Returned legacy metadata has `metadata_source="user_declared"`; V1 has no separate output contract and its `output_contract_sha256` stays null. `require_supported_policy(metadata)` accepts the current V2 identity and rejects V1, unknown or inconsistent policies.

```python
from incfinder import load_output_metadata, require_supported_policy

metadata = load_output_metadata("results/current/inc_label_predictions.csv")
require_supported_policy(metadata)
# Native rejection is represented by gate_pass, not the final Inc_classified flag.
```

`migrate_v1_results(source_dir, destination_dir)` accepts a complete successful software-0.3.0 V1 result directory: six results, `prediction.json` and `run_summary.json`. It verifies the frozen scientific contract, probabilities, masks, gates, label order, calls, native counts, CSV rows and XLSX contents. Unsupported, missing, malformed or inconsistent input is rejected before publishing the destination. Source links and existing destinations are refused. Relative paths resolve against the caller's CWD; software/model/resource directories cannot be overwritten.

The new directory contains the V2 results and companions, the complete read-only source tree under `00_raw_original/`, and `migration_source_manifest.json` with source paths, sizes, SHA-256 and transformation semantics. A new summary explicitly records `operation="v1_to_v2_output_migration"` and `inference_performed=false`; source execution/scientific status is retained. All four original NPZ arrays keep values, shapes, dtypes and NaN masks; Stage1 and Stage2 CSV bytes are copied unchanged. The two final tables, derived arrays and counts adopt V2 semantics. NPZ/XLSX container bytes may change and are not used as a scientific-equality claim.

```python
from incfinder import migrate_v1_results

summary = migrate_v1_results("results/legacy_v1", "results/migrated_v2")
assert summary["inference_performed"] is False
```

Keep the sidecar with every independently shared file. A file copied alone is intentionally rejected unless its source can be explicitly established. Direct calls to internal worker modules produce intermediate files; use the main prediction API/CLI or migration API for a published directory with metadata. Arbitrary third-party `read_csv`/Excel readers must call the guard explicitly; they are not intercepted by this software.

Implementation references for the file formats: [NumPy loading](https://numpy.org/doc/2.4/reference/generated/numpy.load.html), [NPZ writing](https://numpy.org/doc/2.4/reference/generated/numpy.savez.html), and [openpyxl read-only workbooks](https://openpyxl.readthedocs.io/en/stable/optimized.html).

## Frozen engineering example

| Mode | Rejected | Gate-passing no-call | Called | External Unclassified |
|---|---:|---:|---:|---:|
| ct | 5 | 1 | 5 | 6 |
| u05 | 5 | 3 | 3 | 8 |

These selected 11 records test behavior, not population accuracy. See [independent V2 expectations](../tests/fixtures/expected_output_v2.json) and [immutable fixture identities](../tests/fixtures/README.md).

## 中文说明

预测和独立分类在加载模型或创建结果前核验输出合同。版本、口径标识和摘要取自同一次读取。主进程生成报告前会检查分类结果是否匹配该合同，随后用同一合同写入运行摘要和旁车。显式 V1 迁移也先核验目标 V2 合同。

对外未分类表示最终 21 类 Inc 调用全零，包括第一层拒绝及通过后无命中。两种原因由 internal_status 和 gate_pass 保留，原始概率与 21 类标签不变。Stage1_pred 仍只表示门控；最终分型应读取最终标签表或结果表。预测未分类不等于真值未分类，也不能证明生物学不存在。

See [complete frozen CSV/NPZ/XLSX examples](../examples/expected_outputs/README.md) for both modes.
