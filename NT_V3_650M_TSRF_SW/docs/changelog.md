# Changelog


## 模型分卷安装与恢复校验

新增 `incfinder models verify` 和 `incfinder models install`。安装读取固定清单，支持本地分卷、显式 RF 与 NTv3 下载、逐卷续传、同卷暂存及无覆盖发布。已验证模型直接复用，预测保持离线。

## Unreleased

- Replay a sealed Run with recorded parameters into a new Run, compare file bytes and scientific outputs separately, and verify portable private RO-Crate archives with PROV-O and explicit dependency bindings.

- Export an allowlisted offline share and assess replay from verified assets. Preview and apply local Run retention by plan digest, and replay artifact availability without changing the sealed manifest.

- Bind all four candidate formats, build and acceptance receipts, CycloneDX SBOMs and pinned V1 asset references in a detached-signature release manifest.

- Record raw feature tolerance, exact frozen RF outputs, and final decision agreement by CPU and CUDA runtime, platform, and threshold mode.

- Add a read-only resource survey and check per-volume capacity before inference. Stream input rows through a temporary index, keep checkpoint batch identities, and write prediction arrays in original-row blocks.

- Allow prediction to deliver just CSV and NPZ. Seal the calculation before rendering XLSX, retain verified calculation results on report failure or interruption, and rebuild the workbook independently with a receipt linking it to the source artifacts.

- Inspect sealed prediction lineage at file and record level. Export verified relationships and bounded record pages to JSON and offline HTML.

- Persist verified encoding batches in a per-run SQLite checkpoint ledger. Support explicit resume from a stopped run and cancel managed worker processes while keeping completed batches.

- Seal each new prediction with a recursive artifact inventory and direct lineage. Verify reference archives and relocatable copies of input and external feature files.

- Store prediction lifecycle events per run in SQLite, rebuild derived state from committed events, and reconcile interrupted writers against verified result publication.

- Record each prediction with a UUID, effective parameters and their sources, redacted failure status and optional parent-run lineage. Publish only to an unclaimed output directory.

- Isolate trusted Conda preparation from user profiles and use an approved persistent native Linux Git clone without cleaning its retained model weights.
- Validate trusted Windows and Linux in separate complete attempts, preserving installed-package isolation and each attempt's candidate identity.
- Add Windows/Linux regression, exact wheel/Conda installation checks and explicitly trusted frozen-inference workflows with pinned Actions.
- Gate candidate eligibility on the same clean commit, packages, contracts and workflow attempt; reject missing, stale or conflicting evidence without publishing a release.
- Bind each prediction CSV, XLSX and NPZ to a JSON metadata companion and expose strict, version-aware metadata checks.
- Add explicit V1 directory migration with complete read-only source preservation, cross-format validation and unchanged scientific arrays; retain research-preview status.
- Verify the actual SHA-256 of present frozen assets before refreshing the package manifest or starting any release-builder mode; reject equal-size content changes.
- Preserve the previous manifest and distribution files when asset validation fails, while retaining frozen declarations for omitted external model weights.

## 0.4.0 — NT_V3_650M_TSRF_SW_V2

- Unify external Unclassified as all 21 final Inc calls being zero, covering gate rejection and gate-passing no-call.
- Preserve Unclassified / Inc_no_call / Inc_called internally; add trace fields, external masks and output-policy metadata to tables, NPZ, JSON and API results.
- Reject malformed or missing probabilities instead of treating them as valid no-calls.
- Translate first-party source, help, diagnostics, comments, docstrings and executable examples to English; retain escaped Unicode path tests.
- Add schema 2, independent ct/u05 expectations and regression checks; retain all five original scientific payloads unchanged.
- Update bilingual guides, result examples, English scientific-document filename, package metadata and Docker/install references.
- Remove pip's machine-specific local-source URL from Conda metadata and synchronize its installation record before publication.
- Preserve tracked bytes at the repository root so navigation files also match the source checksum manifest after checkout.

The frozen models, feature encoding, 21-label order, thresholds and original tolerances are unchanged. RF assets are reused from preserved V1; NTv3 retains its fixed official revision. RESEARCH_PREVIEW / NOT_ESTABLISHED remain in force.

### 中文变更摘要

V2 合并对外未分类口径，保留门控和三种内部状态；最终表、数组、JSON、API、说明与测试同步更新。代码及默认提示改为英文，保留中文路径支持。模型、阈值、科学合同及五个冻结测试载荷不变。V1 最终分类字段的含义发生变化，下游应按 schema 2 迁移。
