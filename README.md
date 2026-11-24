# Cli Sync

**One-way folder sync for macOS and Linux. A guided terminal menu for people, a JSON CLI for agents and scripts.**

[简体中文](README.zh-Hans.md) · [Quick start](#quick-start) · [Example](#example-back-up-documents-to-an-sftp-server) · [For agents and scripts](#for-agents-and-scripts) · [Sync behavior](#sync-behavior)

Cli Sync copies a local folder to a server over **SFTP**, **SSH commands**, **FTPS**, or **FTP**. A named task keeps its connection, folders, filters, and schedule together. You can set up a task in the menu and run the same task from a shell script or an AI agent.

| Use case | What you get |
| --- | --- |
| Work across macOS and Linux | The same Bash entry points, task configuration, and command flags. The setup script creates a local Python environment. |
| Run a sync by hand | A terminal menu, a step-by-step setup wizard, a preview, and readable progress and results. |
| Automate a task | Non-interactive task settings, structured JSON, exit codes, and per-task scheduling. |
| Keep a remote copy | Resumable uploads, SHA-256 verification before publishing, and optional remote deletion with safeguards. |

## Quick start

You need Bash, Python **3.10+**, and a server that accepts one of the supported protocols. The setup script can install Python via Homebrew on macOS or through `apt` or `dnf` on Linux where available. Ubuntu 22.04+ is the documented Linux baseline.

```bash
git clone https://github.com/sparrowzhou/cli-sync.git
cd cli-sync
bash sync.sh --setup
bash sync.sh --menu
```

In the menu, choose **6** to create a task, **5** to preview it, then **1** to sync. Choose **2** to start scheduled sync, **4** to check status, and **3** to stop it. On macOS you can also open `sync-menu.command`. On Linux, the same menu works in a terminal without a desktop session.

The menu and command output are in English.

## Example: Back up documents to an SFTP server

Create a task called `docs-backup` for `$HOME/Documents` on a Mac or Linux computer. Point it at a dedicated folder on an SFTP server and leave remote deletion off so locally removed files stay on the server.

1. In the menu, create the task and preview the first run. Check the destination and which files will upload.
2. Start a 20-minute schedule from the menu or run `bash sync.sh --task docs-backup --start 20 --json`.
3. Have an agent run `--status --json` for the task and check `last.status` after each completed round.

## For agents and scripts

Run `bash sync.sh --setup` separately once before consuming JSON. This keeps environment setup messages out of the parser's input. Then create a task with `--set`, passing one flag per setting. This example assumes the host key is already trusted and uses an existing SSH key.

```bash
bash sync.sh --task docs-backup \
  --set host=backup.example.com --set username=backup \
  --set "local_dir=$HOME/Documents" --set remote_dir=/home/backup/documents \
  --set "key_file=$HOME/.ssh/id_ed25519" --set accept_new_host_key=false \
  --test-connection --json
```

For password authentication, pass the password on standard input with `--password-stdin` or configure a `password_file`. Task definitions are stored locally under `tasks/`.

| Operation | Command |
| --- | --- |
| Discover tasks | `bash sync.sh --list --json` |
| Preview changes | `bash sync.sh --task docs-backup --dry-run --json` |
| Apply changes | `bash sync.sh --task docs-backup --json` |
| Recompute both sides' file hashes | `bash sync.sh --task docs-backup --full-verify --json` |
| Start a 20-minute schedule | `bash sync.sh --task docs-backup --start 20 --json` |
| Read progress and the last result | `bash sync.sh --task docs-backup --status --json` |
| Stop the schedule | `bash sync.sh --task docs-backup --stop --json` |

- Parse the single JSON result from stdout. Tables and logs go to stderr.
- Check the exit code and `ok`. Exit code `0` indicates success. Exit code `1` can signal a failed operation or a reported issue, such as a preview finding files that differ.
- Inspect `uploaded`, `deleted`, `delete_blocked`, `upload_failed`, and `verify.unverified` on sync results. For a schedule, inspect `schedule.running`, `schedule.syncing`, and `last.status`.
- Use `--dry-run` to review the upload and delete plan before applying it. Check the plan even if the preview exits with code `1`.

## Sync behavior

- **Direction** — The local folder is the source of truth. Remote deletion is off by default. With deletion enabled, an incomplete scan, a changing local file list, a failed upload, or a failed verification blocks deletion for that round.
- **Delivery** — Uploads are staged and checked with SHA-256 before publication. SFTP uses atomic replacement when the server supports it. Interrupted uploads can resume.
- **Verification** — Routine checks can reuse cached hashes. Scheduled full checks and `--full-verify` recompute local and remote hashes.
- **Scheduling** — Each task has its own background process. It can continue when you close the terminal and stops when the computer reboots.
- **Protocols** — Use SFTP for normal SSH servers. The SSH-command mode covers servers without SFTP. FTPS encrypts FTP connections. Plain FTP sends credentials and file data in clear text.

## Documentation and tests

- [English user manual](MANUAL.en.md)

Run the local regression suite after setup:

```bash
.venv/bin/python -B -m unittest -v
```

## License

Cli Sync is available under the [MIT License](LICENSE). Include the copyright and license notices when distributing copies or substantial portions of the software.
