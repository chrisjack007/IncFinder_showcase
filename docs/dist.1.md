# IncFinder v0.5.0rc2-dist.1

## 首次运行

官方示例使用 **CPU**。官方模型共 **27.36 GB**，首次准备建议在模型盘预留 **60 GB**。首次下载需先用同一个 Hugging Face 账号取得 NTv3 权重和基础实现两个仓库的访问权限。已有校验通过的模型可在**菜单 4 → 模型目录**中选用。[NTv3 权重授权](https://huggingface.co/InstaDeepAI/NTv3_650M_pre) · [基础实现授权](https://huggingface.co/InstaDeepAI/ntv3_base_model)

<!-- dist-table-zh -->
| 平台（x86-64） | 下载启动器 | 打开菜单 |
| --- | --- | --- |
| Windows | [incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe) | 双击下载的程序，按 Enter 选择本机运行。 |
| WSL2 | [incfinder-start-0.5.0rc2-dist.1-linux-amd64](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | 在终端运行 `chmod +x incfinder-start-0.5.0rc2-dist.1-linux-amd64`，再运行 `./incfinder-start-0.5.0rc2-dist.1-linux-amd64`。 |
| 原生 Linux | [incfinder-start-0.5.0rc2-dist.1-linux-amd64](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | 在终端运行 `chmod +x incfinder-start-0.5.0rc2-dist.1-linux-amd64`，再运行 `./incfinder-start-0.5.0rc2-dist.1-linux-amd64`。 |
| Docker | [Windows 宿主](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe) · [Linux / WSL2 宿主](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | 启动 Linux 容器引擎，运行宿主启动器，在运行方式中选择 2。 |
<!-- /dist-table-zh -->

1. 选好运行方式后，选**菜单 2 安装并试跑**。启动器核验软件和模型，用随包 `sample.fasta` 跑一次 CPU 预测，采用 `ct` 阈值、批大小 1、4 线程和 1 个 RF 作业。
2. 在终端找到**官方示例核验通过 / Official example verified: 2/2**、平台和 `ct`。两条记录的顺序、两层概率、缺失位置、门控、21 类判定和 CSV 均须与该平台的签名参考相符。第一条显示 `IncFIA;IncFIB;IncFII`，第二条显示 `Inc Unclassified`，第二层 21 项概率均为 NA。从同屏给出的完整路径打开 `inc_results.csv`。
3. 选**菜单 3 使用**，换入自己的 FASTA 或 FASTQ，并打开本次打印的新 CSV 路径。示例核验结论仅适用于随包的两条记录。自有序列保留 `RESEARCH_PREVIEW / NOT_ESTABLISHED` 标记。

本版使用便携运行环境。RF 单独发行，NTv3 由官方授权来源提供。[软件清单](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/SHA256SUMS) · [签名](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/SHA256SUMS.sig) · [独立 RF 模型](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/rf-0.5.0rc2.1) · [证据索引](distribution-evidence.md) · [高级 Conda 安装](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2)

发行签名公钥指纹 `SHA256:MoIhr1BXeQBxbBFtycnWNica1CWnm/uguMkVGUnJGXY`. [OpenSSH allowed signers](https://github.com/chrisjack007/IncFinder_showcase/blob/2c459af6795eac200a4ccbf45b9537bd2ff792bd/launcher/trust/allowed_signers)

## First run

The example runs on **CPU**. Official model files total **27.36 GB**. Allow **60 GB** of free space on the model drive for initial setup. Your Hugging Face account needs access to both the NTv3 weights and the base implementation for the first download. If you already have verified weights, choose **4 · Config → models** and select that directory. [NTv3 weights](https://huggingface.co/InstaDeepAI/NTv3_650M_pre) · [Base implementation](https://huggingface.co/InstaDeepAI/ntv3_base_model)

<!-- dist-table-en -->
| Platform (x86-64) | Download the launcher | Open the menu |
| --- | --- | --- |
| Windows | [incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe) | Double-click the downloaded program. Press Enter for native. |
| WSL2 | [incfinder-start-0.5.0rc2-dist.1-linux-amd64](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | Run `chmod +x incfinder-start-0.5.0rc2-dist.1-linux-amd64`, then `./incfinder-start-0.5.0rc2-dist.1-linux-amd64`. |
| Native Linux | [incfinder-start-0.5.0rc2-dist.1-linux-amd64](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | Run `chmod +x incfinder-start-0.5.0rc2-dist.1-linux-amd64`, then `./incfinder-start-0.5.0rc2-dist.1-linux-amd64`. |
| Docker | [Windows host](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-windows-amd64.exe) · [Linux / WSL2 host](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/incfinder-start-0.5.0rc2-dist.1-linux-amd64) | Start the Linux container engine. Run the host launcher and choose runtime 2. |
<!-- /dist-table-en -->

1. After choosing the runtime, select **2 · Install + demo**. The launcher verifies the software and models, then runs the bundled `sample.fasta` using CPU, `ct` thresholds, batch size 1, four threads and one RF job.
2. Look for **Official example verified: 2/2** beside your platform and `ct`. Both records, their order, the two probability stages, missing values, gate, 21 class decisions and displayed CSV must match the signed platform reference. The first record reads `IncFIA;IncFIB;IncFII`, the second `Inc Unclassified` with 21 missing stage-two probabilities. Open the full `inc_results.csv` path printed on this screen.
3. Choose **3 · Run** and enter your own FASTA or FASTQ. The launcher prints a new CSV path for this run. The example verification applies only to the two bundled records. Raw-sequence predictions retain `RESEARCH_PREVIEW / NOT_ESTABLISHED`.

This release uses portable runtimes. RF and NTv3 have independent delivery sources. [Checksums](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/SHA256SUMS) · [Signature](https://github.com/chrisjack007/IncFinder_showcase/releases/download/v0.5.0rc2-dist.1/SHA256SUMS.sig) · [RF release](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/rf-0.5.0rc2.1) · [Evidence index](distribution-evidence.md) · [Advanced Conda installation](https://github.com/chrisjack007/IncFinder_showcase/releases/tag/v0.5.0rc2)
