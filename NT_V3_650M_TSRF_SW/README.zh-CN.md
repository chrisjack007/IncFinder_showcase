# IncFinder 0.5.0rc2

科研预览版对每条已有序列提供 Inc 候选标签、概率和运行摘要。Windows 使用 ZIP，WSL2 和原生 Linux 使用 TAR.GZ。解压发行包后，新建独立 Conda 环境。

| Platform | Conda |
|---|---|
| Windows x86-64 | `conda create -n IncFinder_Test1 --file environments/win-64.lock` |
| WSL2 / Linux x86-64 | `conda create -n IncFinder_Test1 --file environments/linux-64.lock` |
| Docker (Linux x86-64) | `docker build -t incfinder-ntv3-tsrf:0.5.0rc2 .` |

在解压目录中执行下列命令。先取得官方 NTv3 访问权并登录 Hugging Face。公开 RF 可匿名获取。

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
Dockerfile 在镜像内另建同名环境。模型和输入使用只读挂载，结果写入独立目录。安装 wheel 时在另建的同名锁定环境内执行 `python -m pip install --no-deps ./dist/wheels/incfinder_ntv3_tsrf-0.5.0rc2-py3-none-any.whl`.

```bash
bash docker/run.sh predict examples/sample.fasta -o results/container --model-dir ./models --allow-encoder-drift
```

训练长度范围为 200–10,000 bp。第一层拒绝时第二层概率为 NA。Inc18 两种模式仅检出 1/326，未检出不足以排除该型。

[Model card](docs/MODEL_CARD.zh-CN.md) · [Research licence](LICENSE) · [Release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2) · [Official checkpoint](https://huggingface.co/InstaDeepAI/NTv3_650M_pre/tree/5e6050bed864a5a8fb32481096bf555495316b31) · [Official implementation](https://huggingface.co/InstaDeepAI/ntv3_base_model/tree/27682ed2f537a4c4fcc356b6b8201e7dfceca602) · [Output tables](docs/output_policy.md)
