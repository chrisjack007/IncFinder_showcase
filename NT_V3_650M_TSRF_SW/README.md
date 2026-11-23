# IncFinder 0.5.0rc2

This research preview returns Inc candidate labels, probabilities and a run summary per sequenced record. Extract the Windows ZIP on Windows or the Linux TAR.GZ on native Linux or WSL2. Create a new environment before installing IncFinder.

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
bash docker/run.sh predict examples/sample.fasta -o results/container --model-dir ./models --allow-encoder-drift
```

Training lengths were 200–10,000 bp. Stage-1 rejection yields NA for every stage-2 probability. Both modes detected 1/326 Inc18 positives, so a negative result cannot exclude Inc18.

[Model card](docs/MODEL_CARD.en.md) · [Research licence](LICENSE) · [Release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2) · [Official checkpoint](https://huggingface.co/InstaDeepAI/NTv3_650M_pre/tree/5e6050bed864a5a8fb32481096bf555495316b31) · [Official implementation](https://huggingface.co/InstaDeepAI/ntv3_base_model/tree/27682ed2f537a4c4fcc356b6b8201e7dfceca602) · [Output tables](docs/output_policy.md)
