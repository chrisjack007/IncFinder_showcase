# 自动回归与候选发行门禁

[英文完整说明](README.md)。本流程供维护者使用；软件继续为 0.4.0、输出 schema 2，不修改冻结科学输入或现有 V2 Release。

普通 `ci.yml` 在 Windows/Linux 运行全部单元测试和工作流语法检查，构建一次 wheel/Conda，并完成四个平台与包格式组合的安装检查。单元环境只有 Python 3.12.15、NumPy、openpyxl、et_xmlfile 和 packaging，不需要模型或 Torch；不能靠跳过测试通过。最终包在源码目录之外安装和检查，拒绝源码遮蔽；pip wheel 来源或 Conda 安装记录的实际包摘要必须一致，不能用相同代码内容冒充另一种格式的安装验证。默认只有仓库内容读取权限，Actions 使用完整提交 SHA，所有运行载荷在系统临时目录。

`candidate.yml` 复用普通回归，在明确配置的可信 runner 上，对原始 wheel、Conda 各运行 ct/u05 冻结特征 RF 集成。需要 `self-hosted`、`incfinder-frozen` 和实际系统 `windows`/`linux` 标签，以及 Bash、Conda、兼容固定 Actions 的 runner 版本和完整模型只读路径。仓库变量 `INCFINDER_CI_MODEL_DIR` 指定该路径；不猜测位置，不下载或上传模型。

普通 PR 不运行可信推理。仓库所有者可在 main 或 `codex/if003-ci-validation` 手动触发。首次工作流尚未进入默认分支时，专用验证分支的 push 可以运行普通检查；可信推理还要求所有者执行、`INCFINDER_CI_APPROVED_SHA` 精确等于用户审核后的完整提交，以及 `INCFINDER_CI_RUNNER_OS=windows/linux`。未配置时跳过可信推理，门禁因缺失证据而失败。配置变量、上传验证分支和注册 runner 都须先取得明确授权；验证后清除单提交授权。GitHub 首次手动工作流的默认分支限制见[官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)。

门禁必须取得当前同一仓库、干净提交、源码清单、合同、GitHub run/attempt 的完整证据：两个平台回归、四个最终包安装、四个可信包格式/阈值模式集成，以及原始候选包。它重新计算包摘要，并将各项证据绑定到完整构建收据与安装软件清单。缺模式、失败、篡改、旧 attempt、其他构建或提交均拒绝。只重跑失败 job 可能留下旧 attempt 证据，应重跑整个候选工作流。

现有驱动只执行 frozen 时，六案例总状态仍为 `INCOMPLETE_OR_FAIL`；CI 单独核对七个数组、dtype/NaN、CSV、XLSX 与六个旁车，不把不完整的六案例验收改成通过，也不运行原编码。科学发布状态仍为 RESEARCH_PREVIEW。

内部收据不是独立签名证明，须来自本次可信工作流。门禁开始前先将 `decision.json` 写为 `eligible=false`，失败不会遗留旧的可发布结果。本地或未提交源码收据不能授权发布。即使门禁通过，结果仍为 `release_published=false`；本流程不创建或替换 Release。私有仓库环境审批功能受套餐限制，不能假定已配置，见[官方文档](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)。

本地入口是 `packaging/ci.py` 的 `unit`、`build`、`installed`、`frozen`、`gate`。完整参数和平台命令见英文说明及仓库根 `.github/workflows/`。使用项目/构建 Conda 环境、当前完整 HEAD SHA 和新的系统临时输出目录。本地通过、YAML lint 和历史 V2 验收不能代替真实 GitHub 运行链接；对应真实 CI 尚未完成时，任务条件保持未测。

## 可信环境隔离与 Linux 原生部署

启动可信 runner 前，将固定版本 Miniforge 26.9.4-2 安装到本次系统临时目录，不初始化 shell、不注册 PATH 或快捷方式。通过 runner 进程变量 `INCFINDER_CI_BOOTSTRAP_PYTHON` 指定其 Python 绝对路径。作业使用临时 condarc、缓存和锁定环境，关闭环境登记，以显式 `conda run --prefix` 执行；不在现有账号运行 `conda init` 或会改写用户配置的安装 Action。

Linux 通过 runner 进程变量 `INCFINDER_CI_SOURCE_DIR` 指定本仓库的持久 Git 克隆，预先检出明确批准的完整提交。完整模型以实体文件保存在克隆的软件目录 `models/ntv3`、`models/rf` 中，仓库变量 `INCFINDER_CI_MODEL_DIR` 指向该原生 `models`。作业检查 origin、完整 SHA、源码状态和模型布局，直接使用持久克隆中的锁、测试驱动和参考载荷；不对其运行 `actions/checkout` 或仓库自动清理。模型入库清单和维护收据放在克隆目录外，不改变跟踪文件。禁止使用 `git clean -x/-X` 或强制重置维护该部署，以免删除忽略的大权重。

Linux 安装 wheel 后，从软件目录执行文档中的 help、doctor 与完整模型 check，模型参数使用 `models`。已安装包的冻结验证仍在源码目录之外运行并拒绝源码遮蔽。临时 runner 停止后保留完整克隆和模型；测试环境及计算输出继续位于系统临时目录。

两可信平台按相同批准提交分两轮完整运行，每端四组。保存每个 attempt 的完整证据后才切换平台与模型变量；门禁只使用本轮原始候选包和四组冻结结果，不混合不同 attempt。结束恢复三个临时仓库变量的原状态，只停止本次 runner 和代理，持久 Linux 源码与模型不参与清理。

## 签名发行证据

候选门禁通过后生成未签名的证据包。保留原始 ZIP、tar.gz、wheel、Conda、源码清单、构建和验收回执、四份 CycloneDX 1.6 SBOM 以及 V1 资产快照。各平台 SBOM 记录实际安装的锁定依赖、候选软件和包内 vendor 文件。证据还记录源码及锁文件摘要、构建环境摘要和工具实际版本。验证器使用 [CycloneDX CLI 0.29.2 官方发行](https://github.com/CycloneDX/cyclonedx-cli/releases/tag/v0.29.2) 的固定平台二进制，先核验 SHA-256，再离线校验模式。

用 GitHub 的实际候选提交、run ID 和完整 attempt 核对证据，默认候选身份为 `candidate-<run-id>-<attempt>`。签名前审核原始候选和回执。发行签名使用专用 Ed25519 私钥，存放在仓库、runner 和证据包之外。通过独立可信渠道核对公钥完整指纹并保存外置 `allowed_signers`，主体为 `incfinder-release`，命名空间约束为 `namespaces="incfinder-release"`。 [OpenSSH 验签说明](https://man.openbsd.org/ssh-keygen.1)

签名和验签命令见[英文指南](README.md#signed-release-evidence)。验签使用独立验证过的指定提交源码，针对下载后固定保存的证据副本执行。验证按原始清单字节、独立信任根、完整文件清单、摘要、构建与安装关系的顺序检查，通过后才使用产物。将签名证据和独立回读收据保存在获准的长期位置。

保留 V1 私有 Release、原标签和 26 项引用的资产 ID。每次候选执行均核对已认证的 Release 和标签，以及每项的名称、大小、SHA-256 和下载地址。缺件诊断包含标签、预期文件和资产 ID，权限与摘要问题分别报告。离线恢复沿用 IF-013 的逐卷及整件核验，使用已有且获准保留的模型。NTv3 使用官方固定修订。工作流通过只读仓库权限获取 V1 快照并生成未签名证据，正式签名与发布由发行所有者分别授权。


# 新候选的研究谱系

研究侧保管获准的输入、可恢复的源码和完整运行证据。发行包使用与实际资产绑定的
小型谱系指针。推理结果的侧车和逐行溯源会返回该指针和实际安装文件身份。

1. 在已审阅的干净研究提交上运行 **python packaging/lineage_run.py bundle
   --repo REPO --commit FULL_SHA --output SOURCE.bundle`。使用 `python
   packaging/lineage_run.py environment --output environment.json** 固定当前 Conda
   和 Python 环境，后续用同一环境运行。
2. 为实际入口提供作业 JSON。字段包括 `schema`（1）、`candidate_id`、
   `run_id`、`stage`、`source_bundle`、`git_commit`、`entrypoint`、
   `environment`、`config`、`parents`、`external_inputs`、`output_paths`
   和 `arguments`。文件引用包含 `root`、`path`、`size_bytes` 和
   `sha256`。路径相对各自存储位置。`parents` 使用前置作业输出的
   `complete.json` 引用。输入和产物角色见 `lineage_run.py`。
   用 `@in:train_features`、`@out:weight` 和 `@config` 将受检文件传给实际入口。
   执行 **python packaging/lineage_run.py run --spec job.json --location
   candidate=EVIDENCE --location inputs=INPUTS**。每次尝试在
   `runs/STAGE/ATTEMPT/` 单独存放参数、日志和终态回执。
3. 建立索引 JSON，包含 `schema`（1）、`candidate_id`、`stages` 和 `licenses`。
   每项许可登记 `mode`（`bundle`、`upstream` 或 `internal`）、`scope` 和 `basis` 文件引用。
   依次登记 `prepare`、`encode_train`、`encode_holdout`、`train_stage1`、
   `train_stage2`、`thresholds`、`evaluate`、`report` 的完成回执引用。
   特征行与输入行映射使用带 `record_id` 列的 UTF-8 TSV。两层训练和阈值选择读取训练集，
   评估使用固定留出集与已定版的评估协议。按原语义归档完整概率及行列说明。
   在归档恢复目录运行 **python packaging/lineage_run.py verify --index INDEX
   --location candidate=RESTORED_EVIDENCE --location inputs=RESTORED_INPUTS
   --write-pointer POINTER**，生成验收所用的指针。
4. 把通过校验的指针登记为对应软件候选的 `config/model_lineage.json`。
   两层权重、编码器和阈值的摘要须与 `config/assets.json` 一致。
   候选 RF 权重摘要不同时，冻结验收会先比对原编码资产，再在临时目录给原冻结特征生成本次验收的元数据。原概率、门控、NA 和调用的判定口径沿用既有门禁。
   冻结输出验收通过后，在 `release_evidence.py prepare` 追加
   `--lineage-index INDEX` 和恢复位置，以便发行门禁核对实际证据。
   核对单独保存的结果和侧车时，使用 `release_evidence.py verify --result RESULT`
   及既有的发行身份和外置信任根参数。提供 `--lineage-index` 与恢复位置即可一并核验研究链。

原始快照与上游编码器发布物须有固定版本和逐件校验依据。研究回执与恢复位置保存在获准的
归档中。双语模型卡记录本候选的实测数据、两层拟合、评估结果和分发依据。

带谱系指针的候选在 CI 完成原有候选检查。下载同一次工作流尝试的构建、验收和门禁回执，
在有权读取研究归档的主机上生成待签名证据。

新版模型目录另含四件 NTv3 Python 源码。候选的 `config/model_delivery.json` 使用
`ntv3_code` 登记 `InstaDeepAI/ntv3_base_model` 和修订
`27682ed2f537a4c4fcc356b6b8201e7dfceca602`。安装者取得上游访问权后，
可以把该修订的四件源码和固定检查点的七件文件放在同一输入目录，传给
`incfinder models install` 的 `--ntv3-dir`，或在已登录的环境显式使用
`--download-ntv3`。安装和核验回执列出各件源码的大小、SHA-256 及来源。
加载编码器时再次校验四件源码。模型目录就位后，推理可离线运行。
便携包保留上游许可正文。检查点权重、四个配置文件和上游 README 均随模型从固定修订取得。
