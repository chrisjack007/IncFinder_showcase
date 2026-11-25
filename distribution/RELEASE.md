# dist.1 发行与验收

推理引擎、运行环境和输入参考沿用签名 rc2。附件名称与分发版本由 `launcher/distribution.json` 定义。运行 `python -B distribution/pages.py` 更新平台表，`--check` 用于 CI。Python 打包端使用 3.11 及以上、OpenSSH 和 Go 1.27.4，使用者只需对应宿主启动器。

## 签名与构建

构建输入为原 rc2 完整发行附件与 portable.1 的四个签名归档。RF 独立发行引用原 23 个实体作为上传来源，仅写出新的模型索引。准备与验证命令在仓库根目录执行。命令中的环境变量分别指定两处原附件目录、候选输出根目录和候选提交。受保护的构建机可以把 `INCFINDER_RELEASE_PUBLIC_KEY` 指向登记的公钥，由已授权的 SSH agent 签署。

```sh
python3 -B distribution/pipeline.py build --legacy "$INCFINDER_RC2_ASSETS" --portable "$INCFINDER_PORTABLE_ASSETS" --out "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA" --layout split --key "$INCFINDER_RELEASE_PUBLIC_KEY"
```

同一个 Linux 归档通过 WSL2 和原生 Linux 实机验收后可选择 `--layout shared`。构建在干净提交上核对签名科学清单内的工程提交、四端验收和公开源码摘要。可用 `--engineering-run ID` 额外核对同一提交的工程候选工作流，记录科学输入、打包源码、两个仓库的提交、编译器和各层摘要。`distribution/release.py` 的 `rf`、`packages`、`launchers`、`verify` 子命令支持在工作区分别构建和复查。签名顺序为 `rf-index.json`、`internal/RUNTIME-SHA256SUMS`、软件发行的 `SHA256SUMS`。索引签名覆盖分片、整件、NTv3 代码及权重。内层清单与签名编入 Go 程序，外层清单再覆盖最终附件。

## 草稿、发布与回查

受保护工作流按所选步骤运行。签名、远程写入及不可变设置分别按维护者授权执行。首发可在本地干净签出上运行相同的命令。若先在 GitHub 启用工作流，可单独部署工作流文件，再从批准的候选分支触发。构建机提供已登记的签名身份、可读取工程工作流的凭据和持久化输出目录。普通 PR 运行只读检查。候选源码先发往发行分支，预发行标签指向该分支的精确提交。四份公开首跑回执通过后，再将候选首页和两份 README 合入展示仓库的 main。

```sh
python3 -B distribution/pipeline.py immutability
python3 -B distribution/pipeline.py draft --out "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA" --legacy "$INCFINDER_RC2_ASSETS" --component rf
python3 -B distribution/pipeline.py publish --out "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA" --legacy "$INCFINDER_RC2_ASSETS" --component rf
python3 -B distribution/pipeline.py draft --out "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA" --legacy "$INCFINDER_RC2_ASSETS" --component software
python3 -B distribution/pipeline.py publish --out "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA" --legacy "$INCFINDER_RC2_ASSETS" --component software
```

管理员获准启用仓库设置时，使用 `immutability --enable`。上传可重试，已存在且摘要一致的文件会被复用。附件冲突会终止操作。发布后核对 GitHub 发行证明与匿名附件清单。已公开的不可变候选如需修正，须采用新标签。

## 四端首次安装

四台测试环境分别使用已获准的 Hugging Face 登录和独立空目录。Docker 将创建独立模型卷，授权仍使用既有认证卷。以下命令分别在 Windows、WSL2、原生 Linux 和 Docker 宿主运行，`PLATFORM` 取 `windows`、`wsl2`、`linux` 或 `docker`。验收机需要 Python 和 OpenSSH，新用户的预测运行通过便携启动器完成。

```sh
python3 -B distribution/acceptance.py run --platform "$PLATFORM" --work "$EMPTY_WORKDIR"
```

每个回执含下载 URL、外层清单与运行包摘要、平台参考摘要，以及三次不同预测的运行 ID、记录顺序与 CSV 路径。四份回执统一验证后切换首页。带 `--assets` 的本机测试会标记为 `local`，可指定已有 `--models` 和 `--runtime`。测试记录独立保存在开发记录中。

```sh
python3 -B distribution/acceptance.py gate --release-dir "$INCFINDER_DIST_OUTPUT/$GITHUB_SHA/v0.5.0rc2-dist.1" "$RECEIPTS/windows.json" "$RECEIPTS/wsl2.json" "$RECEIPTS/linux.json" "$RECEIPTS/docker.json"
```

上线后的五位首次使用者从公开首页独立跑官方示例并更换输入。观察记录按主动操作、授权、下载和首次求助分栏，四人独立完成为首轮体验目标。

首页切换后运行 `python3 -B distribution/acceptance.py published-pages --home https://chrisjack007.github.io/IncFinder_showcase/` 核对公开首页和双语入口。
