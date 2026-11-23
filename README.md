# IncFinder 0.5.0rc2

This research preview returns Inc candidate labels, probabilities and a run summary per sequenced record. Extract the Windows ZIP on Windows or the Linux TAR.GZ on native Linux or WSL2. Create a new environment before installing IncFinder.

## Verify downloads

Download `SHA256SUMS`, `SHA256SUMS.sig` and the required assets from the same prerelease. The repository contains the release trust file at `licenses/incfinder-release-allowed-signers`. Its registered key fingerprint is `SHA256:MoIhr1BXeQBxbBFtycnWNica1CWnm/uguMkVGUnJGXY`. Save the trust file as `allowed_signers` and run the following in the download directory with Bash or Windows Git Bash.

```bash
ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
```

Check the output for the exact archive you are installing and its `OK` result. The model installer and verifier check every RF part and the reconstructed model digests.

## Install

| Platform | Conda |
|---|---|
| Windows x86-64 | `conda create -n IncFinder_Test1 --file environments/win-64.lock` |
| WSL2 / Linux x86-64 | `conda create -n IncFinder_Test1 --file environments/linux-64.lock` |
| Docker (Linux x86-64) | `docker build -t incfinder-ntv3-tsrf:0.5.0rc2 .` |

Run these commands from the extracted archive. Obtain official access to NTv3 and log into Hugging Face before downloading it. The public release delivers both RFs.

```bash
conda activate IncFinder_Test1
conda install --offline ./dist/channel/noarch/incfinder-0.5.0rc2-py_0.conda
incfinder version
incfinder models install --model-dir ./models --download-rf --download-ntv3
incfinder models verify --model-dir ./models
incfinder predict examples/sample.fasta --model-dir ./models --allow-encoder-drift --output results/demo
incfinder predict examples/sample.fastq --model-dir ./models --allow-encoder-drift --threshold-mode u05 --output results/demo-u05
incfinder lineage results/demo --row 0
```
The Dockerfile creates the named Conda environment inside the image. Mount models and inputs read-only and write results to a separate directory. For the wheel, create a separate environment from the same platform lock, then run `python -m pip install --no-deps ./dist/wheels/incfinder_ntv3_tsrf-0.5.0rc2-py3-none-any.whl`.

```bash
docker run --rm incfinder-ntv3-tsrf:0.5.0rc2 version
```

Training lengths were 200–10,000 bp. Stage-1 rejection yields NA for every stage-2 probability. Both modes detected 1/326 Inc18 positives, so a negative result cannot exclude Inc18.

On WSL2, write result directories on the distribution’s native Linux filesystem, such as `~/incfinder-results`. DrvFS returned EINVAL during directory publication on the tested Windows mount. Docker can use a Linux data volume for results.


For Docker on a Windows host, publish results to a Linux volume. Run the example below in a Bash shell from the extracted root containing `models` and `examples`.

```bash
host_root="$(pwd -P)"
case "$(uname -s)" in MINGW*|MSYS*) host_root="$(cygpath -m "$PWD")" ;; esac
docker volume create incfinder-results
MSYS_NO_PATHCONV=1 docker run --name incfinder-demo --network none \
  --mount "type=bind,source=$host_root/models,target=/models,readonly" \
  --mount "type=bind,source=$host_root/examples,target=/inputs,readonly" \
  --mount type=volume,source=incfinder-results,target=/results \
  incfinder-ntv3-tsrf:0.5.0rc2 predict /inputs/sample.fasta \
  --model-dir /models --allow-encoder-drift --output /results/demo
docker cp incfinder-demo:/results/demo ./demo
```

[Model card](docs/MODEL_CARD.en.md) · [Research licence](NT_V3_650M_TSRF_SW/LICENSE) · [Release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2) · [Official checkpoint](https://huggingface.co/InstaDeepAI/NTv3_650M_pre/tree/5e6050bed864a5a8fb32481096bf555495316b31) · [Official implementation](https://huggingface.co/InstaDeepAI/ntv3_base_model/tree/27682ed2f537a4c4fcc356b6b8201e7dfceca602) · [Output tables](NT_V3_650M_TSRF_SW/docs/output_policy.md)
