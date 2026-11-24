# Cli Sync User Guide

Cli Sync copies folders from this computer to servers (SFTP, SSH, FTPS or FTP), one way: this computer is the source of truth. You can create several tasks, for example task 1 to server 1 and task 2 to server 2. Each task can also sync on a schedule. Every setting is entered in the menu, so you never edit a file by hand.

## 1. Open the menu

- **macOS**: double-click `sync-menu.command`
- **Ubuntu**: the first time, run `bash menu.sh` as a normal user (not root). After that, type `sftpsync` in any folder.

The first start installs what it needs (Python and its packages). On Ubuntu it may ask once for your login password. After `sftpsync` is installed, log in again or open a new terminal window before using it.

The menu and command output are in English.

## 2. The menu

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

For each task the menu shows:

- **Schedule**: off, or the interval, when it started, and when the next round runs. While a round is running, the live progress is shown.
- **Last**: the time and result of the last sync (✅ / ⚠️ / ❌).
- The local folder, the server folder, and the filter, if any.

Press Enter alone to refresh. To run an action, type its number and press Enter. For actions 1–5 and V you then type the task numbers: separate several with spaces, or type `a` for all. With only one task there is nothing to choose.

## 3. New task (6)

Answer the questions one by one. The value in brackets is the current one; press Enter to keep it.

1. **Task name**, for example `site`.
2. **Server**: first the connection method: SFTP (recommended), SSH commands only (routers and similar devices you can ssh into but without SFTP), FTPS (encrypted FTP), or FTP (unencrypted; use only on your own LAN). Then the address, port (usually 22 for SSH, 21 for FTP), username, and the login method: a password, a private key, a password kept in the Keychain (the desktop keyring on Ubuntu), or a password in a separate file.
3. **Folders**: the local folder (you can drag it into the terminal window) and the folder on the server (created if missing).
4. **Rules**: whether files deleted locally are also deleted on the server, and which files or folders to skip (for example `node_modules .build`).
5. **Filter** (optional): sync only some suffixes (for example `.zip`) or names containing a word, or skip some suffixes or words. Leave everything empty to sync all files.
6. **Interval**: how many minutes between rounds when scheduled sync is on.
7. **Advanced options** (optional; answer y to "Change advanced options"): upload speed limit, compression, parallel connections, when to upload again, symbolic links, how long to wait after a file changes, host keys, SHA-256 method, hours between full checks, connection timeout.

At the end the settings are shown and the connection is tested. The result is a table: the server, whether login works, and whether the server folder already exists (only checked; nothing on the server is changed). If it fails, you can go back and fix the settings, or save anyway.

## 4. Edit or delete a task

- **Edit (7)**: the same questions as for a new task; press Enter for anything you don't want to change. The change takes effect at once. If the task is on scheduled sync, it restarts with the new settings.
- **Delete (8)**: removes only the task's settings on this computer. Files on the server stay.

## 5. Scheduled sync

- Start it with 2 and stop it with 3. The first round runs right away, then one round per interval.
- It keeps running after you close the window. It stops when the computer restarts; start it again after a reboot.
- Each task has its own switch. Nothing syncs while the computer sleeps; it continues after wake-up.

## 6. Results

- After a sync a summary is shown: ✅ all done; ⚠️ some files had problems (they are listed); ❌ the sync failed (the reason is shown).
- During uploads a line refreshes every second with size, speed and time left.
- The menu shows the time and result of the last sync. Choose 4 for details.

## 7. View log (9)

Choose a task; the last 40 lines of its log are shown. Then type:

| Input | What it does |
|---|---|
| Enter | Back to the menu |
| `m` | 200 more lines (repeat as needed) |
| `f` | Follow: new lines appear as they are written, useful to watch a scheduled sync. Ctrl+C stops |
| `a` | Open the whole log in less: arrow keys to scroll, `/` to search, `q` to quit |
| `r` | Refresh |

## 8. Command line (for scripts and agents)

The menu is all you normally need. For scripts, or when an AI agent does the work, everything in the menu is also one command. Run these in the project folder:

| Command | What it does |
|---|---|
| `bash sync.sh --list` | List all tasks and their status |
| `bash sync.sh --task NAME` | Sync once now (`--task` can be left out when there is only one task) |
| `bash sync.sh --task NAME --dry-run` | Dry run: list what would be uploaded, change nothing |
| `bash sync.sh --task NAME --full-verify` | Sync once and recompute local and server file hashes |
| `bash sync.sh --task NAME --start` | Start scheduled sync (`--start 5` = every 5 minutes) |
| `bash sync.sh --task NAME --stop` | Stop scheduled sync |
| `bash sync.sh --task NAME --status` | Scheduled sync status and last result |
| `bash sync.sh --task NAME --set KEY=VALUE …` | Create or edit a task (created if missing), see below |
| `bash sync.sh --task OLD --rename NEW` | Rename a task; its log is renamed too, and a running scheduled sync restarts under the new name |
| `bash sync.sh --task NAME --delete-task --yes` | Delete the task settings; files on the server, the local folder and the log stay |

**Create or edit a task (`--set`)**: use as many `--set` as you need; only those settings change. The same checks as in the menu run before saving; if something is wrong, nothing is saved and the reason is shown.

```bash
echo 'login password' | bash sync.sh --task nas-backup --password-stdin \
  --set host=backup.example.com --set username=me \
  --set local_dir=~/Pictures --set remote_dir=/home/me/pictures-backup --test-connection
bash sync.sh --task nas-backup --set interval_minutes=30 --set only_suffixes=.jpg,.mov
```

| Item | Notes |
|---|---|
| Required for a new task | `host`, `username`, `local_dir`, `remote_dir`, plus one login method |
| Login method (one of four) | `--password-stdin` (reads one line from standard input), `key_file`, `password_file`, `keychain_service`. Passwords cannot go in `--set`, because they would stay in the shell history |
| `--test-connection` | Connect to the server before saving: checks the login and whether the server folder already exists (only checked; the server is not changed). The result is shown as a table. If login fails, nothing is saved and the reason is shown. A changed host key is not asked about; it simply fails. To accept a new host key, edit the task in the menu |
| Other settings | `protocol`, `port`, `delete_remote_extra`, `excludes`, `only_suffixes`, `only_keywords`, `skip_suffixes`, `skip_keywords`, `interval_minutes`, `bwlimit_kbps`, `compress`, `workers`, `upload_when`, `verify_method`, `full_every_hours` and more; the full list is in the README |
| Values | Switches take true or false; lists are separated by commas or spaces |

**Results for programs (`--json`)**: add `--json` to any command. Standard output then holds a single JSON result, and the tables and log lines go to standard error. The exit code is 0 on success, 1 on failure or problems. Times include the time zone, e.g. `2026-11-23T14:10:52-0800`. With `--test-connection`, the JSON includes the result of each check, and when the connection fails the reason is in `error`. See [For agents and scripts](README.md#for-agents-and-scripts) for examples and key result fields.

## How files are compared

- If the server can run commands, hashes are computed there (MD5 when a router only has `md5sum`). If an FTP server supports hash commands, it computes them. Otherwise files are downloaded and hashed locally, but only new uploads and files changed on the server; every file is downloaded once every 24 hours.

- If the modification time or size differs, the file is uploaded again and published after the staged copy passes SHA-256 verification.
- If both match, the SHA-256 is still compared. The same content is skipped; different content is uploaded again. A server file that has not been touched since the last check keeps its previous result instead of being hashed every round; every file is hashed again once every 24 hours.
- Interrupted uploads can resume. If the resumed file fails verification, the next attempt starts from the beginning.
- The task's advanced options can set an upload speed limit and turn on compression. Choose V in the menu to recompute local and server file hashes.

## Common problems

| Problem | What to do |
|---|---|
| macOS says the developer cannot be verified | Right-click `sync-menu.command` → Open |
| A dialog asks whether Terminal may access a folder | Click Allow |
| The connection test says login failed | Try `ssh user@address` first. If ssh fails too, the username or password is wrong |
| Cannot connect to the server | Check the address, port and network |
| FTP says too many connections (421) | Set "parallel upload connections" to 1 in the task's advanced options |
| FTPS says the server does not support encryption | The device has no FTPS. If you use it only on your own LAN, switch the connection method to FTP |
| "Nothing deleted: … above the safety limit" | The local folder is empty, or more than half of the server's files would be deleted. Check the local folder first (is the external drive connected?). To delete them, choose 1 in the menu to sync this task now and confirm |
| Chinese file names break on FTP | In the task's advanced options, set the server's file name encoding to GBK |
| "Not verified" | Reading a server file failed (usually a network glitch). It is checked again next round; look at the log if it keeps happening |
| "Logged in, but SFTP is not available" | Logging in with ssh does not guarantee SFTP. Ask the server administrator to fix one of the causes listed in the message; most often SFTP is not enabled, or a login file such as `.bashrc` prints text |
| Ubuntu says Permission denied | Open it with `bash menu.sh`, not as root |
| `sftpsync`: command not found | Log in again; if that does not help, run `bash menu.sh` once |
| "The server's host key differs from the saved one" | This happens after the server is reinstalled or its key changes. When you are sure it is fine, run the `ssh-keygen -R ...` command shown, then sync again |
| Local files changed during the sync | They are marked in the summary; just sync again |

## Logs

One log file per task, named after the task:

- macOS: `~/Library/Logs/sftp-sync/TASK.log`
- Ubuntu: `~/.local/state/sftp-sync/TASK.log`
