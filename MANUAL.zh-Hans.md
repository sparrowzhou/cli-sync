# Cli Sync 使用手册

把电脑上的文件夹单向同步到服务器（以本地为准）。可以建多个任务，例如任务 1 同步到服务器 1，任务 2 同步到服务器 2，每个任务可以单独开定时同步。所有设置都在菜单里一问一答填写，不用改任何文件。

## 1. 打开菜单

- **macOS**：双击 `sync-menu.command`
- **Ubuntu**：第一次用普通用户（不要用 root）运行 `bash menu.sh`；之后在任何目录直接输 `sftpsync`

第一次打开会自动安装需要的东西（Python 和依赖包），Ubuntu 上可能要输一次登录密码。`sftpsync` 第一次装好后，要重新登录或新开终端窗口才能用。

程序的菜单和命令输出均使用英文。

## 2. 菜单

```
 3 tasks, 2 on scheduled sync

 [1] site    Schedule: ✅ every 10 min, next 10:03 (in 9:57)    Last: 01-15 09:53 ✅
     /Users/me/Documents/site → backup@backup.example.com:/home/backup/site
     Scheduled sync running since 01-15 09:53
 [2] dist    Schedule: ✅ every 30 min, syncing now    Last: 01-15 09:20 ✅
     /home/me/project/dist → backup@server.example.net:/home/backup/dist
     Filter: only .zip
     Uploading 0/1  24 MB / 96 MB  25%  8 MB/s  ETA 0:09  example-release.zip
 [3] backup    Schedule: off    Last: 01-14 18:00 ✅
------------------------------------------
  1) Sync now
  2) Start scheduled sync
  3) Stop scheduled sync
  4) Status and last result
  5) Dry run: list what would be uploaded, change nothing
  V) Full check: sync once and recompute local and server file hashes
  6) New task
  7) Edit task
  8) Delete task
  9) View log
  0) Quit
```

每个任务显示：
- **定时同步**：关，或者每几分钟一次、从什么时候开始运行、下次什么时候同步（还剩多久）；正在同步时显示实时进度
- **上次**：最近一次同步的时间和结果（✅ / ⚠️ / ❌）
- 本地文件夹 → 服务器目录，以及筛选规则

直接回车刷新。输数字回车执行操作；选 1～5 或 V 时再输任务编号：多个用空格隔开，`a` 表示全部；只有一个任务时不用选。

## 3. 新建任务（选 6）

按提示一项项填，括号里是当前值，直接回车就保留：

1. **任务名称**：自己起，例如 `site`
2. **服务器**：先选连接方式——SFTP（推荐）、只用 SSH 命令（能 ssh 登录但没有 SFTP 的路由器等）、FTPS（加密的 FTP）、FTP（不加密，只建议在自家局域网里用）；再填地址、端口（SSH 一般是 22，FTP 一般是 21）、用户名，选择登录方式：密码、私钥、密码存在钥匙串（Ubuntu 是桌面密钥环）里，或密码放在单独的文件里
3. **文件夹**：本地文件夹（可以把文件夹拖进终端窗口）、服务器上的目录（不存在会自动创建）
4. **同步规则**：本地删掉的文件服务器上要不要也删；哪些文件或文件夹不同步（例如 `node_modules .build`）
5. **筛选**（可选）：只同步某些后缀（例如 `.zip`）或文件名含某些字的文件；也可以跳过某些后缀或关键字。都不填就是全部同步
6. **定时间隔**：开定时同步时每隔几分钟同步一次
7. **高级选项**（可选，问「要修改高级选项吗」时输 y）：上传限速、压缩传输、同时上传的连接数、什么时候重新上传、符号链接怎么处理、刚修改的文件等几秒再传、服务器指纹、SHA-256 校验方式、每隔几小时全部重算一次、连接超时

填完会显示一遍设置，并测试能不能连上服务器。结果是一张表格：服务器、能否登录、服务器目录是否已存在（只查看，不会改动服务器）。连不上可以回去改，也可以先保存。

## 4. 修改、删除任务

- **修改（选 7）**：和新建一样一项项过，只改需要改的，其他直接回车。保存后马上生效；这个任务正在定时同步的话，会自动按新设置重新开始。
- **删除（选 8）**：只删除这台电脑上的任务设置，服务器上的文件不受影响。

## 5. 定时同步

- 选 2 开始，选 3 停止。开始后先马上同步一轮，之后按设定的间隔自动跑。
- 关掉窗口也会继续运行；电脑重启后会停止，需要再开一次。
- 每个任务各自开关，互不影响。电脑睡眠时不同步，醒来后继续。

## 6. 看结果

- 立即同步结束后显示汇总：✅ 全部成功；⚠️ 个别文件有问题，会列出来；❌ 没同步成功，会写原因。
- 上传时有一行每秒刷新的进度：大小、速度、剩余时间。
- 菜单里每个任务后面会显示上次同步的时间和结果。详细内容选 4 查看。

## 7. 查看日志（选 9）

选一个任务，显示它日志的最后 40 行。下面可以接着输：

| 输入 | 作用 |
|---|---|
| 回车 | 返回菜单 |
| `m` | 往前多看 200 行（可以连续输） |
| `f` | 实时查看：有新日志就自动显示，适合看定时同步正在做什么；按 Ctrl+C 停止 |
| `a` | 用 less 打开全部日志，方向键翻页，`/` 搜索，`q` 退出 |
| `r` | 刷新 |

## 8. 命令行（给脚本和 agent 用）

平时用菜单就够了。写脚本、或者让 AI agent 来操作时，菜单里的每件事都可以用一条命令完成。在 Cli Sync 项目文件夹里运行：

| 命令 | 作用 |
|---|---|
| `bash sync.sh --list` | 列出所有任务和它们的状态 |
| `bash sync.sh --task 任务名` | 立即同步一次（只有一个任务时可以不写 `--task`） |
| `bash sync.sh --task 任务名 --dry-run` | 演练：只看要传哪些文件，不改服务器 |
| `bash sync.sh --task 任务名 --full-verify` | 同步一次，重算本地和服务器文件的哈希 |
| `bash sync.sh --task 任务名 --start` | 开始定时同步（`--start 5` = 每 5 分钟一次） |
| `bash sync.sh --task 任务名 --stop` | 停止定时同步 |
| `bash sync.sh --task 任务名 --status` | 查看定时同步状态和上次结果 |
| `bash sync.sh --task 任务名 --set 键=值 …` | 新建或修改任务（没有这个任务就新建），见下面 |
| `bash sync.sh --task 旧名 --rename 新名` | 给任务改名，日志一起改名；正在定时同步的会按新名字重新开始 |
| `bash sync.sh --task 任务名 --delete-task --yes` | 删除任务设置；服务器上的文件、本地文件夹和日志都不动 |

**新建或修改任务（`--set`）**：可以写多个 `--set`，只改写到的项。保存前做和菜单里一样的检查，有问题就不保存，并说明原因。

```bash
echo '登录密码' | bash sync.sh --task nas备份 --password-stdin \
  --set host=backup.example.com --set username=me \
  --set local_dir=~/照片 --set remote_dir=/home/me/照片备份 --test-connection
bash sync.sh --task nas备份 --set interval_minutes=30 --set only_suffixes=.jpg,.mov
```

| 项 | 说明 |
|---|---|
| 新建时必填 | `host`、`username`、`local_dir`、`remote_dir`，再加一种登录方式 |
| 登录方式（四选一） | `--password-stdin`（从输入读一行密码）、`key_file`、`password_file`、`keychain_service`。密码不能写在 `--set` 里，因为会留在命令历史里 |
| `--test-connection` | 保存前先连一次服务器，检查能否登录、服务器目录是否已存在（只查看，不改服务器），结果显示成表格。登录失败就不保存，并说明原因。服务器指纹变了时不会提问，直接算失败；要换成新指纹，请在菜单里修改这个任务 |
| 其他设置项 | `protocol`、`port`、`delete_remote_extra`、`excludes`、`only_suffixes`、`only_keywords`、`skip_suffixes`、`skip_keywords`、`interval_minutes`、`bwlimit_kbps`、`compress`、`workers`、`upload_when`、`verify_method`、`full_every_hours` 等，完整列表见 README |
| 值的写法 | 开关填 true 或 false；清单用逗号或空格隔开 |

**给程序读的结果（`--json`）**：任何一条命令后面加 `--json`，屏幕输出（stdout）只有一份 JSON 结果，表格和日志改到错误输出（stderr）。命令运行成功退出码是 0，失败或有问题是 1。时间都是带时区的格式，例如 `2026-11-23T14:10:52-0800`。加了 `--test-connection` 时，JSON 里有每项检查的结果；连不上时，失败原因直接写在 `error` 里。命令示例和主要字段见[供 Agent 和脚本使用](README.zh-Hans.md#供-agent-和脚本使用)。

## 同步规则

- 服务器能执行命令时，在服务器上算 hash（路由器上只有 `md5sum` 时用 MD5）；FTP 服务器支持 hash 命令时让它算；都不行时下载回来算，只下载刚上传的和服务器上被动过的文件，每 24 小时才全部下载一次。

- 修改时间或大小不一致的文件：重新上传，暂存文件通过 SHA-256 校验后才发布。
- 修改时间和大小都一致的文件：再核对 SHA-256，内容一致就跳过，不一致就重新上传。服务器上的文件自上次核对后没被动过，就沿用上次的结果，不用每轮都重算；每 24 小时全部重算一次。
- 传输中断后可续传，续传文件校验失败时会从头重传。
- 任务设置的高级选项里可以设上传限速、打开压缩传输。在菜单里选 V 可重算本地和服务器文件的哈希。

## 常见问题

| 现象 | 怎么办 |
|---|---|
| macOS 提示"无法验证开发者" | 在 `sync-menu.command` 上右键 →「打开」 |
| 弹窗问"终端"能否访问文件夹 | 点「允许」 |
| 测试连接提示登录失败 | 先用 `ssh 用户名@地址` 试一下。ssh 也登不上，就是账号密码不对 |
| 连不上服务器 | 检查地址、端口和网络 |
| FTP 提示连接数过多（421） | 在任务高级选项里把「同时上传的连接数」调成 1 |
| FTPS 提示服务器不支持加密 | 设备不支持 FTPS。只在自家局域网里用的话，把连接方式改成 FTP |
| 结果提示「没有删除：……超过安全上限」 | 本地文件夹是空的，或者要删服务器上一半以上的文件。先确认本地文件夹没问题（移动硬盘有没有接上）；确实要删，在菜单里选 1 立即同步这个任务，按提示确认 |
| FTP 上中文文件名出错或乱码 | 在任务高级选项里把「服务器上文件名的编码」改成 GBK |
| 提示「没能核对」 | 读取服务器文件时出错（多半是网络闪断），下一轮会再核对，不用处理；一直出现的话看日志 |
| 提示「已经登录上服务器，但用不了 SFTP」 | 能 ssh 登录不代表一定能用 SFTP。按提示里列的原因请服务器管理员处理，最常见的是服务器没开 SFTP，或者登录启动文件（如 `.bashrc`）会输出文字 |
| Ubuntu 提示 Permission denied | 用 `bash menu.sh` 打开，不要用 root |
| 输 `sftpsync` 提示找不到命令 | 重新登录一次；还不行就运行一次 `bash menu.sh` |
| 提示"服务器指纹和上次记录的不一样" | 服务器重装过或换过密钥时会这样。确认没问题后，运行提示里的 `ssh-keygen -R ...` 命令，再同步一次 |
| 同步过程中本地文件又被改了 | 汇总里会标出来，再同步一次即可 |

## 日志

每个任务一个日志文件，文件名就是任务名：

- macOS：`~/Library/Logs/sftp-sync/任务名.log`
- Ubuntu：`~/.local/state/sftp-sync/任务名.log`
