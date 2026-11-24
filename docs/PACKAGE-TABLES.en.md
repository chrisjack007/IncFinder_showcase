# Reading the package tables

[简体中文](PACKAGE-TABLES.zh-CN.md) · [Repository guide](../README.md)

The installation, output and validation tables below identify the software version and test set used. For rc2 results on the independent holdout, read the model card's [overall performance](MODEL_CARD.en.md#aggregate) and [per-label results](MODEL_CARD.en.md#per-label). Both packaged model cards contain those two tables. [Packaged English card](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/MODEL_CARD.en.md) · [Packaged Chinese card](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/MODEL_CARD.zh-CN.md)

<a id="install"></a>

## Installation commands

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/README.md?plain=1#L5) · [Chinese original](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/README.zh-CN.md?plain=1#L5)

Choose one original rc2 platform row. The first column names the environment and the second gives the command to run in the extracted package root. Windows and Linux use their respective Conda locks. WSL2 shares the Linux lock with native Linux. The Docker row builds the Linux image.

| Platform | Setup command |
|---|---|
| Windows x86-64 | `conda create -n IncFinder_Test1 --file environments/win-64.lock` |
| WSL2 / Linux x86-64 | `conda create -n IncFinder_Test1 --file environments/linux-64.lock` |
| Docker (Linux x86-64) | `docker build -t incfinder-ntv3-tsrf:0.5.0rc2 .` |

<a id="options"></a>

## Prediction options

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/usage.md?plain=1#L44)

Each row is an input or option for `incfinder predict`. The left column preserves the command-line spelling. The right gives its meaning and default where applicable. `--threads` counts encoder threads, `--jobs` counts RF jobs and `--batch-size` counts records per encoder batch.

| Parameter | Meaning and default |
|---|---|
| Positional input or `-i / --i / --input` | Supply one file, optionally gzip-compressed. Choose either the positional form or a named input option. |
| `-o / --o / --output` | Choose a new results directory. |
| `--model-dir` | Model directory containing `ntv3/` and `rf/`. |
| `--device` | Defaults to `cpu`. Select `cuda` explicitly in an environment with the necessary GPU dependencies. The archived parameter table records CPU validation. |
| `--threads` | Encoder threads, default 4. |
| `--jobs` | Random forest jobs, default 1. |
| `--batch-size` | Records per encoder batch, default 1. |
| `--threshold-mode` | Defaults to `ct`. Select `u05` for the other frozen threshold mode. |
| `--features-dir` | Directory containing `features.npy` and its `features.json`. IDs, order, format, sequence digests and the model contract must match. |
| `--allow-encoder-drift` | Required preview opt-in for raw sequences because encoder equivalence remains unestablished. |
| `--max-length` | Length limit in bp, default 10000. Overlong inputs are never silently truncated. |
| `--allow-out-of-domain` | Explicitly accepts extrapolation outside the training length range. Performance there remains unvalidated. |
| `--parent-run-id` | Optional ID of a completed parent run in the same run-record directory. |

<a id="conditions"></a>

## V2 output states

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/output_policy.md?plain=1#L7)

Each row follows one record through a possible decision path. The first column states the gate and stage-two conditions. `internal_status` keeps the native state. `Inc_unclassified` and `Inc_classified` are complementary final flags, with 1 meaning true. `Result` is the displayed text or labels. Rejection leaves all 21 stage-two probabilities missing. A gate-passing no-call has valid stage-two probabilities.

| Native condition | internal_status | Inc_unclassified | Inc_classified | Result |
|---|---|---:|---:|---|
| Stage1 < 0.5 | Unclassified | 1 | 0 | Inc Unclassified |
| Stage1 >= 0.5, no Stage2 call | Inc_no_call | 1 | 0 | Inc Unclassified |
| Stage1 >= 0.5, one or more calls | Inc_called | 0 | 1 | Semicolon-separated labels |

<a id="calls"></a>

## V2 calls on the 11-record test set

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/output_policy.md?plain=1#L72)

Each row applies one threshold mode to the same 11 frozen records. **Rejected** counts records below the stage-one gate. **Gate-passing no-call** counts those passing the gate with no label at threshold. **Called** counts records with at least one label. These three counts sum to 11. **External Unclassified** combines the first two, for example 5 + 1 = 6 in `ct`.

| Mode | Rejected | Gate-passing no-call | Called | External Unclassified |
|---|---:|---:|---:|---:|
| ct | 5 | 1 | 5 | 6 |
| u05 | 5 | 3 | 3 | 8 |

<a id="platforms"></a>

## V2 platform comparisons

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/validation.md?plain=1#L9)

Each row is an operating system and device. The 11 and 82 in the headings are record counts for two test sets. The first two checks compare re-encoded features to V2 and IF-004 CPU respectively, requiring the same order, finite values and tolerances `atol=1e-4`, `rtol=1e-5`. The **frozen RF** pair checks exact agreement of both layers' probabilities and missing-value masks on fixed features. The **raw decisions** pair compares gates, masks and all 21 calls to frozen-input predictions on the same platform.

**PASS** means the stated comparison met its criterion, **FAIL** means a measured comparison missed it and **NOT_TESTED** means that check did not run in that environment. Read down a column to compare the same test across platforms. This matrix covers software 0.4.0 at commit `f743f03`.

| Context | 11 raw features vs V2 | 82 raw features vs IF-004 CPU | 11 frozen RF | 82 frozen RF | 11 raw decisions | 82 raw decisions |
| --- | --- | --- | --- | --- | --- | --- |
| Windows native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Windows native RTX 5070 Ti | FAIL | FAIL | NOT_TESTED | NOT_TESTED | PASS | PASS |
| Linux native CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Linux native Tesla V100 | FAIL | PASS | NOT_TESTED | NOT_TESTED | PASS | PASS |
| WSL2 Ubuntu CPU | FAIL | PASS | PASS | PASS | PASS | PASS |
| Docker CPU | FAIL | PASS | PASS | PASS | PASS | PASS |

<a id="checks"></a>

## V2 release checks

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/docs/validation.md?plain=1#L26)

Each row is one software 0.4.0 (V2) acceptance check. The first column names what was checked. The second gives the outcome and scope in that row's units. When reading decision counts, the external pair is unclassified/classified and the native triple is rejected/no-call/called. Each grouping describes the same records.

| Check | Result and measured scope |
|---|---|
| Unit tests | 53/53 passed from source and from the installed Windows package. |
| Frozen assets | All 14 original SHA-256 digests matched across 25,904,505,766 bytes. |
| Original scientific fixtures | All 5 files matched their originals, totaling 102,462 bytes. |
| Runtime packages | The wheel and Conda package shared 27 identical code and resource files that also matched the final source. |
| Windows install | The rebuilt package was installed in an independent Python 3.12 environment created from `win-64.lock`. |
| WSL install | An existing isolated Linux environment created from the lock was upgraded to the rebuilt package. |
| Docker install | The image was built with the pinned official base digest, the Linux lock and the same Conda package. |
| Frozen `ct` and `u05` | Windows, WSL and Docker matched the original probabilities, NaN masks, gates and calls in both modes. |
| Public and native decisions | `ct` gave external counts of 6/5 and native counts of 5/1/5. `u05` gave 8/3 and 5/3/3 respectively. |
| Cross-platform outputs | All 7 NPZ arrays, 4 CSV tables and the Excel cell contents agreed within each mode. |
| Windows formats | FASTA, repeat FASTA, FASTQ, FASTA.gz and FASTQ.gz passed, with identical RF arrays, states and tables. |
| Python API | On the same supplied features, the API and CLI returned the same arrays and tables. The summary matched JSON and the caller kept its working directory and environment. |
| Failure guards | The preview opt-in, overwrite refusal with existing files intact, and invalid model and sequence checks passed. |
| English code | First-party source and executable documentation examples passed the Chinese-character scan. The Unicode path tests were retained. |

<a id="files"></a>

## Five frozen test files

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/tests/fixtures/README.md?plain=1#L12)

Each row identifies one frozen file. The first column gives its filename, **Bytes** its exact size and **SHA-256** the digest of all its bytes. The five sizes sum to 102,462 bytes. Compare each file's own digest to the value in that row.

| Payload | Bytes | SHA-256 |
|---|---:|---|
| expected.npz | 2204 | d0bbb31faf9b7a6fee8c13e9b271a0a75dc01bae08853580e5f0c792f68fb03d |
| features.json | 3572 | 7c55087a8cef6083a55c70fbf03dcb8020f5ee41f5afa4bdad6f6afdf478dbbc |
| features.npy | 67712 | 5f8bc5a7a46f6167c3e1e3db20808ed2572cfe84a786a32691879c43df69e96f |
| reference.fasta | 28357 | d77da8445d8ea189b9b64cc827e601ca6e29ba22401711a55b215ca97c29b336 |
| source_rows.json | 617 | 4f8f256dc34761d0b1cda442b23c06ca09d8da76ced2085ddc11a7d7fc7fcb13 |

<a id="arrays"></a>

## Arrays for the 11 test records

[Original package table](https://github.com/chrisjack007/IncFinder_showcase/blob/v0.5.0rc2/NT_V3_650M_TSRF_SW/tests/fixtures/README.md?plain=1#L34)

Each row is an array. **Array** identifies the file or NPZ member, **Shape** gives the length of each dimension in order, **dtype** is the element type and **Meaning** explains the values. The first dimension is the same 11 ordered records. A second dimension of 1,536 indexes features and 21 indexes labels. `bool` stores true/false, `uint8` stores the 0/1 calls here and `float32` stores features or probabilities.

| Array | Shape | dtype | Meaning |
|---|---|---|---|
| features.npy | (11,1536) | float32 | Rows follow `reference.fasta` and columns are encoder dimensions. |
| expected.npz:stage1 | (11,) | float32 | Stage-one positive-class probability for each record. |
| expected.npz:stage2 | (11,21) | float32 | Columns follow the labels in `contract.json`. A rejected record has a full row of NaN values. |
| expected.npz:gate | (11,) | bool | True when the stage-one probability is ≥ 0.5. |
| expected.npz:labels | (11,21) | uint8 | Binary label calls (0/1) under the frozen `ct` thresholds. |
