# Cli Sync

**在 macOS 和 Linux 上单向同步文件夹。人可以用向导式终端菜单，Agent 和脚本可以用 JSON 命令行。**

[English](README.md) · [快速开始](#快速开始) · [使用案例](#案例把文档备份到-sftp-服务器) · [供 Agent 和脚本使用](#供-agent-和脚本使用) · [同步规则](#同步规则)

Cli Sync 通过 **SFTP**、**SSH 命令**、**FTPS** 或 **FTP** 把本地文件夹同步到服务器。每个命名任务保存自己的连接、目录、筛选规则和同步间隔。可以在菜单里设置任务，然后让脚本或 Agent 运行同一个任务。

| 使用场景 | 功能 |
| --- | --- |
| 在 macOS 和 Linux 上使用 | 两个平台使用相同的 Bash 入口、任务设置和命令选项。安装脚本会创建本地 Python 环境。 |
| 手动运行同步 | 终端菜单、配置向导、演练，以及可直接阅读的进度和结果。 |
| 自动运行任务 | 非交互式任务配置、结构化 JSON、退出码和独立的任务调度。 |
| 维护远端副本 | 支持断点续传，发布前校验 SHA-256，可选启用带保护的远端删除。 |

## 快速开始

需要 Bash、Python **3.10+**，以及支持其中一种协议的服务器。安装脚本可以通过 macOS 上的 Homebrew 或 Linux 上的 `apt`、`dnf` 安装 Python。Linux 文档以 Ubuntu 22.04 及以上为基线。

```bash
git clone https://github.com/sparrowzhou/cli-sync.git
cd cli-sync
bash sync.sh --setup
bash sync.sh --menu
```

菜单里选 **6** 创建任务，选 **5** 演练，选 **1** 正式同步。选 **2** 开启定时同步，选 **4** 查看状态，选 **3** 停止。macOS 也可以双击 `sync-menu.command`。Linux 上无需桌面环境，同一个菜单可直接在终端使用。

程序的菜单和命令输出均使用英文。

## 案例：把文档备份到 SFTP 服务器

在 Mac 或 Linux 上为 `$HOME/Documents` 创建名为 `docs-backup` 的任务，目标指向 SFTP 服务器上的专用目录。保持远端删除关闭，这样本机删除文件后，服务器上的副本仍会保留。

1. 在菜单里创建任务并演练，确认目标目录和首次待上传的文件。
2. 在菜单里设置每 20 分钟同步一次，或运行 `bash sync.sh --task docs-backup --start 20 --json`。
3. 让 Agent 对该任务运行 `--status --json`，在每轮结束后检查 `last.status`。

## 供 Agent 和脚本使用

首次使用前，先单独运行一次 `bash sync.sh --setup`，避免环境准备消息进入 JSON 解析器。接下来，每项任务设置用一个 `--set` 传入。这个示例使用已有的 SSH 私钥，并假定服务器的主机密钥已受信任。

```bash
bash sync.sh --task docs-backup \
  --set host=backup.example.com --set username=backup \
  --set "local_dir=$HOME/Documents" --set remote_dir=/home/backup/documents \
  --set "key_file=$HOME/.ssh/id_ed25519" --set accept_new_host_key=false \
  --test-connection --json
```

使用密码登录时，可以通过 `--password-stdin` 从标准输入传入密码，或配置 `password_file`。任务设置保存在本机的 `tasks/` 目录。

| 操作 | 命令 |
| --- | --- |
| 查看任务 | `bash sync.sh --list --json` |
| 演练同步 | `bash sync.sh --task docs-backup --dry-run --json` |
| 执行同步 | `bash sync.sh --task docs-backup --json` |
| 重算本地和远端的文件哈希 | `bash sync.sh --task docs-backup --full-verify --json` |
| 开启每 20 分钟一次的同步 | `bash sync.sh --task docs-backup --start 20 --json` |
| 查看进度和上次结果 | `bash sync.sh --task docs-backup --status --json` |
| 停止定时同步 | `bash sync.sh --task docs-backup --stop --json` |

- 从标准输出解析一份 JSON 结果。表格和日志输出到标准错误。
- 同时检查退出码和 `ok`。退出码 `0` 表示成功，`1` 可以表示操作失败或发现问题，例如演练发现文件有差异。
- 同步结果中可检查 `uploaded`、`deleted`、`delete_blocked`、`upload_failed` 和 `verify.unverified`。定时同步可检查 `schedule.running`、`schedule.syncing` 和 `last.status`。
- 执行同步前可用 `--dry-run` 查看上传和删除计划。演练退出码为 `1` 时，也应检查计划内容。

## 同步规则

- **方向** — 以本地目录为准，默认关闭远端删除。启用删除后，如果本地扫描不完整、文件列表发生变化、上传失败或校验失败，本轮会跳过远端删除。
- **传输** — 文件先上传到暂存位置，通过 SHA-256 校验后再发布。SFTP 在服务器支持时使用原子替换。上传中断后可以续传。
- **校验** — 常规校验可以复用缓存的哈希。定期全量校验和 `--full-verify` 会重算本地和远端的哈希。
- **定时运行** — 每个任务有独立的后台进程。关闭终端后仍可继续运行，电脑重启后停止。
- **协议** — 常规 SSH 服务器使用 SFTP；服务器未提供 SFTP 时，可以使用 SSH 命令模式。FTPS 加密 FTP 连接，普通 FTP 会以明文传输登录凭据和文件。

## 文档和测试

- [简体中文使用手册](MANUAL.zh-Hans.md)

完成安装后，可运行本地回归测试：

```bash
.venv/bin/python -B -m unittest -v
```

## 许可证

Cli Sync 采用 [MIT 许可证](LICENSE)。分发软件副本或其实质部分时，必须保留版权声明和许可证文本。
