# CI and candidate eligibility

[Chinese guide](README.zh-CN.md). This is a maintainer workflow. It leaves software 0.4.0, output schema 2, frozen science and the existing V2 Release unchanged.

## Ordinary regression

The repository-root `.github/workflows/ci.yml` runs Windows/Linux unit regressions, workflow lint, one wheel/Conda build and four installed-package checks. It can run on PRs, main pushes, manual dispatch or as a reusable workflow. It has `contents: read`, no model binding and no self-hosted job. Actions are pinned to complete upstream commit SHAs. Actionlint 1.7.12 is pinned and its download SHA-256 is checked.

The minimal unit environment is `environments/ci-tests.yml`: Conda Python 3.12.15, NumPy, openpyxl, et_xmlfile and packaging. Tests must complete without skips; their discovered count is recorded instead of using a hard-coded total. The separate build environment contains the pinned build tools. Installed checks use the existing OS lock, the original candidate artifact, an external CWD and the active environment's installed package; source shadowing is rejected. The pip wheel origin or Conda prefix record must identify the actual artifact hash, so identical code payloads cannot substitute one installation format for another.

All stage outputs must be under the process system-temp directory, outside the checkout. Jobs set TMPDIR/TEMP/TMP to RUNNER_TEMP. `.github` configuration stays out of software distribution manifests. No workflow uploads model files or complete build workspaces; uploads name only packages, receipts and selected logs.

## Trusted candidate workflow

`candidate.yml` reuses ordinary regression, then checks both original package formats in ct and u05 on one explicitly configured self-hosted runner. Required runner labels are `self-hosted`, `incfinder-frozen` and `windows` or `linux`. The runner needs a compatible current GitHub runner/Node24 Actions runtime, Bash, Conda and access to the complete immutable model directory. The existing OS lock recreates the runtime. The full weights are verified locally and remain outside GitHub artifacts.

Repository variable `INCFINDER_CI_MODEL_DIR` gives the complete model directory on that runner. There is no implicit path or model download. Manual execution is restricted to the repository owner and main or `codex/if003-ci-validation`.

For initial testing before these files exist on main, the candidate workflow also accepts a push to that exact validation branch. Ordinary checks can run immediately. A self-hosted job additionally requires owner execution and repository variables `INCFINDER_CI_APPROVED_SHA` equal to the complete, explicitly reviewed pushed commit and `INCFINDER_CI_RUNNER_OS` equal to `windows` or `linux`. Missing values skip trusted inference and the final gate fails for missing evidence. These variables and any runner registration must be explicitly authorized before configuration. Clear the one-commit approval after validation.

This bootstrap path is necessary because new `workflow_dispatch` workflows initially need their file on the default branch; [GitHub's event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch) describes the restriction. After landing on main, use manual dispatch and select the trusted OS.

The existing six-case acceptance driver is reused with only `--cases frozen`. Its six-case `engineering_status` remains `INCOMPLETE_OR_FAIL`. CI separately requires all seven reference arrays, masks/dtypes, four CSVs, XLSX cells and six sidecars to pass. It never reclassifies an incomplete six-case run as complete. No raw encoder run or scientific-status upgrade occurs.

### Isolated runtime and persistent Linux clone

Before starting a trusted runner, install the pinned Miniforge 26.9.4-2 into its task-specific system-temp directory without shell initialization, PATH registration or shortcuts. Set the runner-process variable `INCFINDER_CI_BOOTSTRAP_PYTHON` to that installation's absolute Python executable. The job uses a temporary condarc, package cache and locked runtime, disables environment registration and calls `conda run --prefix` explicitly. It never runs `conda init` or the profile-rewriting setup action on a self-hosted account.

For Linux, set the runner-process variable `INCFINDER_CI_SOURCE_DIR` to a persistent, normal Git clone of this repository, already checked out at the exact approved commit. Keep the complete model files physically inside `NT_V3_650M_TSRF_SW/models/ntv3` and `models/rf`, and point `INCFINDER_CI_MODEL_DIR` there. The job requires the expected origin, commit, clean tracked source and native model layout. Linux uses that clone's lock, test driver and reference files; it does not run `actions/checkout` or repository-cleaning commands against the persistent deployment. Keep model import manifests and maintenance receipts outside the clone, so its tracked files remain unchanged. Never use `git clean -x`, `git clean -X` or forced resets to maintain this deployment: ignored weights must survive later tests.

After installing the wheel, Linux also runs the documented help, doctor and full model check from the software directory with `--model-dir models`. Frozen installed-package checks still run outside the checkout and reject source shadowing. The clone and models remain available after the ephemeral runner stops; task environments and inference outputs remain in system temp.

To validate both trusted platforms, run Windows and Linux separately at the same approved source commit. Archive each complete attempt before switching the platform/model variables and rerunning all jobs. Each gate checks its own four frozen combinations and current candidate bytes; evidence from different attempts is never combined. Restore the three temporary repository variables to their previous state afterward. Only this task's runner and proxy processes are stopped; persistent Linux source and models are excluded from cleanup.

## Gate evidence

`packaging/release_gate.py` requires exactly:

- One successful build receipt and the original wheel and Conda bytes, rehashed at gate time.
- Two successful platform regression receipts and four installed package/platform receipts.
- Four trusted frozen receipts: wheel/Conda times ct/u05 on one actual supported trusted platform.

Every receipt binds the same repository, clean source commit, source manifest, three contract identities, GitHub run and attempt. Installed/frozen receipts bind the complete build-receipt hash, package hash and installed software manifest. A previous run/attempt, a changed build receipt, a missing mode, source shadowing, failed checks or tampering blocks eligibility. Re-running only failed jobs can leave evidence from an older attempt; rerun the whole candidate workflow to obtain a coherent set.

Receipt schema 1 is internal CI evidence, distinct from scientific run identity. Receipts are not independently authenticated signatures; the workflow must obtain them from its current trusted run. Local or dirty-tree receipts deliberately cannot authorize publication. `decision.json` is replaced with `eligible=false` before validation, so failure cannot retain an old positive decision.

The only successful gate result is candidate eligibility with `scientific_release_status=RESEARCH_PREVIEW` and `release_published=false`. These workflows never create or replace a GitHub Release. Private-repository environment approval availability depends on the account plan; [GitHub's environment documentation](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) is authoritative. No absent environment approval is assumed to exist.

## Local commands

Run with the appropriate project/build Conda Python, from the repository checkout. The commit must match HEAD; source manifests are refreshed before regression/build. Use new temp stage directories. For example, in Bash:

```bash
module="$PWD/NT_V3_650M_TSRF_SW"
commit=$(git rev-parse HEAD)
python -B "$module/packaging/ci.py" unit --source-sha "$commit" --output "$TMPDIR/unit"
# In environments/ci-build.yml:
python -B "$module/packaging/ci.py" build --source-sha "$commit" --output "$TMPDIR/candidate"
```

Install the exact artifact into a separate environment, change to its temp working directory and run `installed` or `frozen` with `--source-sha`, `--build-receipt`, `--artifact`, `--package-kind` and a new `--output`. Frozen checks additionally require `--model-dir` and `--threshold-mode`. The workflow files contain the complete platform commands. Local receipts explicitly record `execution_context=local`, run ID 0 and the actual dirty/clean source state. A local pass or YAML lint is not evidence that GitHub jobs completed; retain actual run links before accepting CI requirements.

## Signed release evidence

On a successful candidate run, the gate produces an unsigned release evidence bundle. Keep the exact ZIP, tar.gz, wheel, Conda package, source manifest, build/gate/install/frozen and unit receipts, four CycloneDX 1.6 SBOMs, and V1 asset snapshot together. Each platform SBOM inventories the packages from the exact installed Conda lock, the actual candidate distribution and packaged vendor files. The checkout lock digests, build environment digest and actual build-tool versions are bound into the evidence. CycloneDX CLI 0.29.2 validates the SBOMs offline after the platform binary SHA-256 has been checked. Obtain the appropriate binary from the [pinned upstream release](https://github.com/CycloneDX/cyclonedx-cli/releases/tag/v0.29.2).

Use the verified candidate repository, complete commit SHA, run ID, and full run attempt from GitHub. The default candidate identity is `candidate-<run-id>-<attempt>`. Review the unsigned bundle and run receipts before signing. Only the designated release signer uses the dedicated Ed25519 private key. Store that key outside the checkout, runner and artifact bundle. Distribute the trusted public key and its full fingerprint through a separately verified channel. Create the external `allowed_signers` file there with the principal `incfinder-release` and namespace constraint `namespaces="incfinder-release"`. [OpenSSH signing and verification](https://man.openbsd.org/ssh-keygen.1)

```bash
ssh-keygen -Y sign -n incfinder-release -f "$RELEASE_SIGNING_KEY" "$BUNDLE/release-manifest.json"
python -B "$TRUSTED_CHECKOUT/NT_V3_650M_TSRF_SW/packaging/release_evidence.py" verify \
  --bundle "$DOWNLOADED_BUNDLE" --trust-file "$ALLOWED_SIGNERS" --cyclonedx "$PINNED_CYCLONEDX" \
  --release "candidate-$RUN_ID-$RUN_ATTEMPT" --repository chrisjack007/IncFinder_engineer \
  --source-commit "$SOURCE_SHA" --run-id "$RUN_ID" --run-attempt "$RUN_ATTEMPT"
```

Use a separately verified checkout of the specified commit as the verifier. Run against a stable downloaded copy outside that checkout before using its packages. Verification checks the original manifest bytes, the externally supplied trust file, exact file inventory, signed SHA-256 values, and the original candidate gate and install bindings. A bundled key, stale attempt, changed receipt, replaced archive or mismatched SBOM blocks verification. Archive the signed evidence and independent readback receipt in the approved retention location.

Retain the original private V1 Release, tag, and 26 referenced asset IDs. The current candidate workflow reads the authenticated release and tag identities and compares each name, byte count, SHA-256 and fixed download URL with the pins. A missing asset reports the exact tag, filename and asset ID. Access failures and metadata mismatches receive separate diagnostics. For offline recovery use the IF-013 verified ordered parts and an authorized retained model copy while preserving the original part and whole-file hashes. The separately licensed NTv3 revision remains pinned to its official provider. The workflow uses repository read access for the V1 snapshot and emits unsigned evidence. Release signing and publishing require the release owner's separate authorization.


# Research lineage for a new candidate

Keep the research source, approved datasets and outputs in the research area. The
release build consumes a small hash-bound pointer to the research evidence. The
same pointer appears in the inference result sidecars and the row lineage output.

1. Check out the reviewed research commit and create a recoverable Git bundle
   with `python packaging/lineage_run.py bundle --repo REPO --commit FULL_SHA --output SOURCE.bundle`.
   Capture the active Conda/Python environment with **python packaging/lineage_run.py
   environment --output environment.json**. Use the same interpreter to run the job.
2. Run each reviewed Python entry with **python packaging/lineage_run.py run --spec job.json
   --location candidate=EVIDENCE --location inputs=INPUTS**. The JSON spec declares
   `schema` (1), `candidate_id`, `run_id`, `stage`, `source_bundle`,
   `git_commit`, `entrypoint` (Git-relative Python path), `environment`,
   `config`, `parents`, `external_inputs`, `output_paths` and `arguments`.
   A file reference has `root`, `path`, `size_bytes` and `sha256`.
   Locations are supplied separately and the paths in references are relative to
   them. Parent entries map stage names to the previous `complete.json` references
   printed by the launcher. The required input and output roles are in `lineage_run.py`.
   Use arguments such as `@in:train_features`, `@out:weight` and `@config`.
   Under `runs/STAGE/ATTEMPT/` the launcher leaves the exact parameters, job log
   and one completion or failure receipt; an interrupted job without a complete
   receipt can be retried with a new attempt.
3. Create the index with `schema` (1), `candidate_id` and `stages` (one
   complete receipt reference per stage) and `licenses`. Each license entry lists
   `mode` (`bundle`, `upstream`, or `internal`), `scope`, and a `basis` file reference.
   The stages are `prepare`, `encode_train`,
   `encode_holdout`, `train_stage1`, `train_stage2`, `thresholds`, `evaluate` and
   `report`. Supply UTF-8, tab-separated metadata and feature-row maps with a
   `record_id` column. The training entries consume the training split only,
   whereas evaluation reads the locked holdout inputs and frozen evaluation protocol.
   Keep the raw snapshot, row mappings, reports, full scores and their layout with their
   evidence. Run **python packaging/lineage_run.py verify --index INDEX --location
   candidate=RESTORED_EVIDENCE --location inputs=RESTORED_INPUTS
   --write-pointer POINTER** from a storage location that can restore the original bytes.
4. Place the verified pointer at `config/model_lineage.json` in the matching
   software candidate. Its two weights, encoder and threshold table must agree
   with `config/assets.json`. Run the existing frozen-output acceptance before
   assembling release evidence. Add `--lineage-index INDEX` and the same
   `--location` mappings to `release_evidence.py prepare`. The signed release
   verifies the pointer, build and installation receipts together. Verify a result
   plus its adjacent metadata with `release_evidence.py verify --result RESULT`
   and the usual release identity and trust arguments. Supply `--lineage-index`
   and the restore locations to also recover and verify the research chain.

A dated record and constituent checksums are required for any raw snapshot and
upstream encoder release referenced by this chain. Store research receipts and
recovery locations in the approved archive. The matching model card describes the
verified data, fits, evaluation, and permitted scope of distribution.

For traced candidates, download this exact workflow attempt's candidate, evidence
and eligibility receipt to a trusted host with read access to the research
archive. Assemble the unsigned bundle there with the documented lineage flags.

The new model directory also holds the four NTv3 Python sources. The candidate
catalog declares `ntv3_code` with repository `InstaDeepAI/ntv3_base_model` and
revision `27682ed2f537a4c4fcc356b6b8201e7dfceca602`. With upstream access,
supply those four files alongside the seven pinned checkpoint files using
`incfinder models install --ntv3-dir`, or explicitly select `--download-ntv3`
after signing in. The installation and verification receipts record the source,
size and SHA-256 of each file. The encoder verifies all four again before loading.
Inference can then run offline. The portable package retains the licence notice.
The checkpoint weights, four configurations and upstream README come from the pinned model release.
