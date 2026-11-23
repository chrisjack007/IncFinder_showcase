# Scientific scope and portability

The independent 0.5.0rc2 RF evaluation is documented in the [model card](MODEL_CARD.en.md). The experiments below describe the historical encoder and V2 engineering baseline. The installed CLI recognizes the bundled c11 regression as FROZEN_REFERENCE. For c82 or the verified full holdout, pass both `--features-dir` and `--allow-encoder-drift`. The CLI reads those frozen features without invoking the encoder. Compare the results to the rc2 archived arrays.

## Frozen inference contract

The checkpoint is NTv3_650M_pre, revision 5e6050bed864a5a8fb32481096bf555495316b31. The model-code revision, feature semantics, 21 labels, length limits, and asset identities are recorded in config/contract.json and config/assets.json.

Encoding uses FP32 tensors, one token per base, no added special tokens, right N-padding to a multiple of 128, mean pooling over original positions only, and the mean of forward/reverse-complement pooled vectors. Original N bases contribute to pooling; artificial padding does not. No input sequence is silently truncated.

Both RFs are frozen grid_0001 forests with 500 trees. Stage 1 uses >= 0.5; Stage 2 uses fixed training-OOF label thresholds in config/thresholds.tsv (ct), or the explicitly selected u05 mode. No retraining or test-driven threshold selection occurs.

## Research-preview boundary

New-sequence scientific equivalence is NOT_ESTABLISHED. The original reference tolerance is atol=1e-4 and rtol=1e-5. Existing CPU and GPU re-encoding evidence did not pass that tolerance; these limits are not relaxed by this release. Raw-sequence predictions require --allow-encoder-drift and preserve the preview state.

The 11 frozen reference rows are an engineering set. Exact frozen-feature RF probabilities, gates, NA masks, and labels test preservation of the deployed decision pipeline. New encoder differences, cross-platform agreement, and label agreement must be reported separately. None establishes population accuracy, sensitivity, specificity, or performance on real metagenomic reads.

## Independent CPU encoder validation

The research-preview validation version `ntv3_650m_cpu_fp32_v1` specifies CPU FP32, batch size 1, four encoder threads, and RF jobs 1. The [version contract](../config/encoder_versions/ntv3_650m_cpu_fp32_v1.json) records the platform locks, reference digest, and acceptance scope. Current CLI and API entry points retain the existing research-preview flag and output policy.

On the locked retrospective subset of 82 records, Windows and Linux each produced exactly repeatable features. The cross-platform maximum absolute difference was 0.000610351562, with zero elements outside the existing tolerance. Against the paired frozen features, the two RF models gave the same gates, Stage 2 NA masks, and all 21 calls in both ct and u05. The two probability layers varied by up to 0.00400000811 and 0.00999999046, respectively. The independent version retains its own reference digest. The original 11-record benchmark and its scientific-equivalence status retain their existing scope.

## Biological interpretation

Predictions apply to the supplied sequence, not automatically to an entire plasmid, host, sample, or microbial community. No replicon localization or abundance estimation is performed. Training lengths were 200–10000 bp. Shorter or longer inputs require explicit extrapolation flags and have no new accuracy guarantee.

FASTA/FASTQ support is an input interface. FASTQ quality values do not affect the model. The software does not implement read QC, error correction, paired-end merging, assembly, or automatic windows. RF scores have no additional calibration as biological confidence probabilities.

## Portability

Models are separate from installed code and are verified with fixed SHA-256 values before prediction. Source/config/vendor/reference resources use package-relative locations. Modern CLI/API user paths follow the caller's CWD; the legacy launcher retains software-root-relative behavior.

A model location must contain ntv3/ and rf/. Explicit invalid locations fail without fallback. Installed wheels and Conda packages include the small config/vendor/reference resources but exclude weights. Prediction workers use the current interpreter unless legacy interpreters are explicitly supplied.

CPU is the default, without GPU probing. Linux RF workers adjust their file-descriptor soft limit up to 32768 within the unchanged hard limit; the host shell is not changed. Docker prediction disables networking and uses read-only model/input mounts.

V2 release engineering results are in [validation.md](validation.md). The [post-release matrix](validation_matrix.json) binds the original-feature and independent-CPU comparisons to their own references and reports frozen RF and 21-label decisions separately for each measured runtime. The original benchmark IDs, SHA-256 values, revision identifiers, and data-row provenance are retained without publishing private host paths. Upstream license restrictions continue to apply.
