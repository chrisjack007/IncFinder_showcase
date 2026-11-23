# IncFinder 0.5.0rc2

**[推荐下载 0.5.0rc2 科研预览版](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2)**

科研预览版对每条已有序列提供 Inc 候选标签、概率和运行摘要。Windows 使用 ZIP，WSL2 和原生 Linux 使用 TAR.GZ。解压发行包后，新建独立 Conda 环境。

## 核验下载文件

从同一预发行下载 `SHA256SUMS`、`SHA256SUMS.sig` 和所需资产。仓库的 `licenses/incfinder-release-allowed-signers` 保存发行公钥，登记指纹为 `SHA256:MoIhr1BXeQBxbBFtycnWNica1CWnm/uguMkVGUnJGXY`。在 Bash 或 Windows Git Bash 的下载目录运行。将仓库公钥清单保存为 `allowed_signers`。

```bash
ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
```

核对输出包含实际要安装的压缩包并显示 `OK`。两层 RF 的每卷及重组后的整件由 `incfinder models install` 和 `incfinder models verify` 校验。

## 安装

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
docker run --rm incfinder-ntv3-tsrf:0.5.0rc2 version
```

训练长度范围为 200–10,000 bp。第一层拒绝时第二层概率为 NA。Inc18 两种模式仅检出 1/326，未检出不足以排除该型。

WSL2 的输出目录放在发行版自己的 Linux 文件系统中，例如 `~/incfinder-results`。在本次配置中，向 Windows 挂载盘发布已计算的目录时，DrvFS 返回了 EINVAL。Docker 可用 Linux 数据卷保存结果。


Windows 宿主上的 Docker 可将结果写入 Linux 数据卷。下面的命令在含 `models` 和 `examples` 的解压根目录执行，适用于 Bash、WSL2 和 Git Bash。

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

## 下载与安装问题

若 Miniconda 因默认频道的条款提示中止，可在创建环境和安装本地包的两条 Conda 命令中加入 `--override-channels -c conda-forge`。Linux 锁内的 95 项依赖均来自 conda-forge。

若服务器能打开项目首页，但访问 GitHub 下载网页超时，可从[公开发行附件清单](https://api.github.com/repos/chrisjack007/IncFinder_showcase/releases/tags/v0.5.0rc2)选取文件，以 `Accept: application/octet-stream` 请求其 `url`，下载后按上文验签和核对摘要。

如果 GitHub 的匿名 API 配额用尽，可从发行页将 `stage1.joblib` 和 `stage2.joblib.part-001` 至 `part-022` 共 23 个文件下载到同一 `rf-parts` 目录。核对签名清单后执行 `incfinder models install --model-dir ./models --parts-dir ./rf-parts --download-ntv3`。

Windows 可将同名环境建在用户 Conda 目录中。本次验收中，建在系统临时目录的环境一度缺失 Python 标准库文件，在用户 Conda 目录重装后通过。

[Model card](docs/MODEL_CARD.zh-CN.md) · [Research licence](NT_V3_650M_TSRF_SW/LICENSE) · [Release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2) · [Official checkpoint](https://huggingface.co/InstaDeepAI/NTv3_650M_pre/tree/5e6050bed864a5a8fb32481096bf555495316b31) · [Official implementation](https://huggingface.co/InstaDeepAI/ntv3_base_model/tree/27682ed2f537a4c4fcc356b6b8201e7dfceca602) · [Output tables](NT_V3_650M_TSRF_SW/docs/output_policy.md)
