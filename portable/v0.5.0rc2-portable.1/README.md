# IncFinder portable · 校验与科研资料

本目录属于 v0.5.0rc2-portable.1。四个运行包均使用已签名的 IncFinder 0.5.0rc2、原训练权重和 21 项正式阈值。

| 下载包 | 字节数 | SHA-256 |
| --- | ---: | --- |
| IncFinder-Windows-x64.zip | 513810320 | `9746515574a90799aa9570777b578e1385c922f5dda679ba8821194d06abeb80` |
| IncFinder-WSL2-x64.tar.gz | 613624771 | `d791130fea29e2bcdfa18e0ed013a776be8ce6526fb1fc33d4e241f108267842` |
| IncFinder-Docker-amd64.zip | 831785250 | `ef4c2bc89bc8dc216f0f681c8ecffcca0389420365c45cd5c1bf6929861d8c00` |
| IncFinder-Linux-x64.tar.gz | 613624674 | `2adf8fefdbb5e759a489dfe33fe1e537ce9a79775c7aa752b6000cb27b5f41fa` |

将 SHA256SUMS、SHA256SUMS.sig、allowed_signers 与下载包放在同一目录。项目登记的公钥指纹为 `SHA256:MoIhr1BXeQBxbBFtycnWNica1CWnm/uguMkVGUnJGXY`。

```sh
ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum --ignore-missing -c SHA256SUMS
```

Windows PowerShell 可用以下命令验签和计算所选文件的摘要。

```powershell
cmd.exe /d /c "ssh-keygen -Y verify -f allowed_signers -I incfinder-release -n incfinder-release -s SHA256SUMS.sig < SHA256SUMS"
Get-FileHash -Algorithm SHA256 .\IncFinder-Windows-x64.zip
```

[中文模型卡](MODEL_CARD.zh-CN.md) · [English model card](MODEL_CARD.en.md) · [21 类指标](per_class_metrics.tsv) · [示例参考](example-reference.json) · [依赖许可](dependency-notices.zip) · [NTv3 许可](MODEL-LICENSE.md)

[Windows SBOM](SBOM-windows.cdx.json) · [WSL2 SBOM](SBOM-wsl2.cdx.json) · [Docker SBOM](SBOM-docker.cdx.json) · [Linux SBOM](SBOM-linux.cdx.json)

The example references and holdout metrics describe the original rc2 scientific model. Each portable archive contains its own SBOM and its copy of the original signed checksum file.
