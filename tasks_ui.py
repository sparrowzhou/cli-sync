"""Interactive menu and task management for Cli Sync."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import paramiko

import sftp_sync as core

SCRIPT = Path(core.__file__).resolve()
BASE = SCRIPT.parent
TASKS = core.TASKS_DIR

DEFAULT_EXCLUDES = ["*.tmp", "node_modules", ".git"]
SECTION_ORDER = ["server", "sync", "filter", "schedule", "verify", "log"]


# ---------------------------------------------------------------- Task files

def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    # TOML basic strings accept the JSON-style escapes used for passwords and paths.
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(raw: dict, name: str) -> str:
    lines = [f"# Settings of sync task \"{name}\". Edit them with the menu (bash menu.sh or sftpsync).", ""]
    sections = [s for s in SECTION_ORDER if s in raw] + [s for s in raw if s not in SECTION_ORDER]
    for sec in sections:
        body = raw[sec]
        if not isinstance(body, dict):
            continue
        items = [(k, v) for k, v in body.items() if v is not None]
        if not items:
            continue
        lines.append(f"[{sec}]")
        lines += [f"{k} = {toml_value(v)}" for k, v in items]
        lines.append("")
    return "\n".join(lines)


def write_task(path: Path, raw: dict) -> None:
    TASKS.mkdir(exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # Task files can contain passwords. Restrict access to the owner.
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(dump_toml(raw, path.stem))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_raw(path: Path) -> dict:
    with open(path, "rb") as f:
        return core.tomllib.load(f)


def load_cfg(path: Path):
    """Return the task configuration or a human-readable error."""
    try:
        return core.load_config(path), ""
    except SystemExit as e:
        return None, str(e)
    except Exception as e:
        return None, f"Settings file is broken: {e}"


# ---------------------------------------------------------------- Input

class Back(Exception):
    """Return to the preceding menu."""


def read_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise Back


def enter_hint(default: str) -> str:
    return f" [Enter = {default}]"


def ask(prompt: str, default: str = "", required: bool = True, check=None, hint: str | None = None) -> str:
    while True:
        h = hint if hint is not None else (enter_hint(default) if default != "" else "")
        v = read_line(f"{prompt}{h}: ").strip()
        if not v:
            v = default
        if v == "" and required:
            print("  " + "This is required")
            continue
        if check and v != "":
            err = check(v)
            if err:
                print(f"  {err}")
                continue
        return v


def ask_yes(prompt: str, default: bool) -> bool:
    while True:
        yn = "Y/n" if default else "y/N"
        v = read_line(f"{prompt}{f' ({yn})'}: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes", "1"):
            return True
        if v in ("n", "no", "0"):
            return False
        print("  " + "Please enter y or n")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    def check(v):
        if not v.isdigit() or not lo <= int(v) <= hi:
            return f"Enter a number from {lo} to {hi}"
    return int(ask(prompt, str(default), check=check))


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    """Return the value selected by its displayed number."""
    core.print_table(["No.", "Option"],
                     [[str(i), label] for i, (_, label) in enumerate(options, 1)])
    keys = [k for k, _ in options]
    d = str(keys.index(default) + 1) if default in keys else ""
    v = ask(prompt, d, check=lambda x: None if x.isdigit() and 1 <= int(x) <= len(options)
            else "Enter one of the numbers above")
    return keys[int(v) - 1]


def ask_list(prompt: str, current: list[str], example: str) -> list[str]:
    cur = ", ".join(current) if current else "(none)"
    print(f"  {prompt}")
    print(f"    {'Current'}: {cur}")
    v = read_line("    " + f"Separate with commas or spaces, e.g. {example}; Enter = keep, - = clear"
                  + ": ").strip()
    if not v:
        return list(current)
    if v == "-":
        return []
    return [x for x in re.split(r"[,\uff0c\u3001\s]+", v) if x]


def clean_path(v: str) -> str:
    """Normalize a path pasted or dragged into the terminal."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    else:
        v = re.sub(r"\\(.)", r"\1", v)
    return os.path.expanduser(v.rstrip("/") or "/")


def pause() -> None:
    try:
        input("\n" + "Press Enter to return to the menu…")
    except EOFError:
        pass


def clear() -> None:
    if sys.stdout.isatty():
        print("\033[H\033[2J", end="")


def with_sigint_ignored(fn):
    """Let the child handle Ctrl+C while the menu waits."""
    old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return fn()
    finally:
        signal.signal(signal.SIGINT, old)


def child_sigint():
    signal.signal(signal.SIGINT, signal.SIG_DFL)


# ---------------------------------------------------------------- Task status

def last_result(cfg) -> str:
    key = core.state_dir() / core.state_key(cfg)
    try:
        d = json.loads(Path(f"{key}.last.json").read_text(encoding="utf-8"))
        icon = {"ok": "✅", "fail": "❌"}.get(d.get("status"), "⚠️")
        return f"{core.fmt_time(d.get('time'))} {icon}"
    except (OSError, ValueError):
        pass
    try:  # Older releases saved only a text summary.
        text = Path(f"{key}.last.txt").read_text(encoding="utf-8")
    except OSError:
        return "never"
    when = re.search(r"\d{4}-(\d\d-\d\d \d\d:\d\d)", text)
    icon = next((line.strip()[0] for line in text.splitlines() if line.strip()[:1] in ("✅", "❌")), "⚠️")
    return f"{when.group(1) if when else ''} {icon}".strip()


def schedule_info(cfg) -> tuple[str, list[str]]:
    """Return the schedule summary and any detail lines."""
    d = core.read_daemon(cfg)
    if not d:
        return "Schedule: off", []
    m = d["minutes"]
    head = f"Schedule: ✅ every {m} min"
    started = core.fmt_time(d.get("started"))
    detail = [f"Scheduled sync running since {started}"]
    if d.get("running"):
        head += ", syncing now"
        try:
            pg = json.loads((core.state_dir() / f"{core.state_key(cfg)}.progress.json").read_text(encoding="utf-8"))
            if time.time() - pg["time"] < 10:
                detail.append(pg["line"])
        except (OSError, ValueError, KeyError):
            detail.append("Scanning and checking files…")
    elif d.get("next_run"):
        left = max(0, int(d["next_run"] - time.time()))
        at = time.strftime("%H:%M", time.localtime(d["next_run"]))
        mm, ss = left // 60, left % 60
        head += f", next {at} (in {mm}:{ss:02d})"
    return head, detail


def task_row(i: int, path: Path) -> list[str]:
    """Build a task status row for the menu."""
    cfg, err = load_cfg(path)
    if not cfg:
        return [str(i), path.stem, f"❗ {err}", "", ""]
    head, detail = schedule_info(cfg)
    head = re.sub(r"^[^\uff1a:]*[\uff1a:]\s*", "", head, count=1)  # Drop the schedule prefix already provided by the column header.
    where = f"{cfg.local_dir}\n→ {cfg.username}@{cfg.host}:{cfg.remote_dir}"
    flt = core.filter_desc(cfg)
    if flt:
        where += f"\n{'Filter'}: {flt}"
    return [str(i), path.stem, "\n".join([head] + detail), last_result(cfg), where]


# ---------------------------------------------------------------- Task wizard

NAME_RE = re.compile(r"^[^/\\:*?\"<>|\s.][^/\\:*?\"<>|\s]{0,39}$")


def check_name(v: str, old: str | None) -> str | None:
    if not NAME_RE.match(v):
        return "No spaces or / \\ : * ? \" < > |, must not start with ., at most 40 characters"
    if v != old and (TASKS / f"{v}.toml").exists():
        return f"A task named \"{v}\" already exists"
    return None


def check_local(v: str) -> str | None:
    p = Path(clean_path(v))
    if not p.is_dir():
        return f"No such folder on this computer: {p} (you can drag the folder into the terminal window)"
    return None


def check_remote(v: str) -> str | None:
    if not v.startswith("/"):
        return "Enter a full path starting with /, e.g. /home/USER/backup"
    return None


def step(n: int, title: str) -> None:
    print(f"\n—— {n}/6 {title} ——")


def save_to_keyring(service: str, account: str) -> None:
    """Prompt through the system credential store so the password stays out of command arguments."""
    if core.IS_MAC:
        print("  " + "You will be asked for the server login password twice (not shown while typing):")
        cmd = ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"]
    else:
        print("  " + "Enter the server login password (not shown while typing):")
        cmd = ["secret-tool", "store", f"--label={service} {account}", "service", service, "account", account]
    while True:
        try:
            r = subprocess.run(cmd)
        except FileNotFoundError:
            print("  ❌ " + "secret-tool not found; run: sudo apt install libsecret-tools, or choose another login method")
            raise Back()
        if r.returncode == 0:
            try:
                core.read_keychain(service, account)
                print("  ✅ " + "Password saved")
                return
            except SystemExit as e:
                print(f"  ❌ {e}")
        if not ask_yes("  " + "Not saved; try again", True):
            raise Back()


def wizard(raw: dict, name: str | None) -> tuple[str, dict]:
    """Prompt for the task name and configuration through the interactive menu."""
    srv = dict(raw.get("server", {}))
    syn = dict(raw.get("sync", {}))
    flt = dict(raw.get("filter", {}))
    sch = dict(raw.get("schedule", {}))
    vf = dict(raw.get("verify", {}))

    step(1, "Task name")
    new_name = ask("Task name (for example, documents-backup)",
                   name or "", check=lambda v: check_name(v, name))

    step(2, "Server")
    old_proto = srv.get("protocol", "sftp")
    print("  " + "Connection method" + ": ")
    proto = ask_choice("  " + "Choose", list(core.PROTOCOLS.items()), old_proto)
    if proto == "ftp" and old_proto != "ftp":
        print("  ⚠️  " + ("Plain FTP is not encrypted: the password and the files travel in plain text. "
                           "Use it only on your own LAN; choose FTPS if the device supports it."))
        if not ask_yes("  " + "Use plain FTP anyway", False):
            proto = "ftps"
    srv["protocol"] = proto
    is_ftp = proto in ("ftp", "ftps")
    srv["host"] = ask("Server address (IP or domain name)", srv.get("host", ""))
    port = int(srv.get("port", core.DEFAULT_PORTS[old_proto]))
    if port == core.DEFAULT_PORTS[old_proto]:
        port = core.DEFAULT_PORTS[proto]  # Recompute the default port after a protocol change.
    srv["port"] = ask_int(("FTP port" if is_ftp else "SSH port"), port, 1, 65535)
    srv["username"] = ask("Login username", srv.get("username", ""))
    if srv.get("password"):
        cur = "password"
    elif srv.get("key_file"):
        cur = "key"
    elif srv.get("keychain_service"):
        cur = "keychain"
    elif srv.get("password_file"):
        cur = "password_file"
    else:
        cur = "password"
    store = "macOS Keychain" if core.IS_MAC else "desktop keyring"
    if is_ftp and cur == "key":
        cur = "password"  # FTP does not support SSH keys.
    opts = [("password", "Password (stored in the task file, readable only by you)")]
    if not is_ftp:
        opts.append(("key", "Private key file"))
    opts += [
            ("keychain", f"Password kept in the {store} (not in the task file)"),
            ("password_file", "Password in a separate file")]
    print("  " + "Login method" + ": ")
    method = ask_choice("  " + "Choose", opts, cur)
    if method == "keychain":
        srv["keychain_service"] = "sftp-sync"
        if cur != "keychain" or ask_yes("  " + f"Enter the password in the {store} again", False):
            save_to_keyring(srv["keychain_service"], srv["username"])
        for k in ("password", "key_file", "key_passphrase", "password_file"):
            srv.pop(k, None)
    elif method == "password_file":
        default_pf = srv.get("password_file") or "~/.config/sftp-sync/password"
        srv["password_file"] = ask("  " + "Password file path (the file holds only the password)",
                                   default_pf, check=lambda v: None if Path(clean_path(v)).is_file() else "File not found; create it first (permissions 600)")
        srv["password_file"] = clean_path(srv["password_file"])
        for k in ("password", "key_file", "key_passphrase", "keychain_service"):
            srv.pop(k, None)
    if method == "password":
        old = srv.get("password") or ""
        while True:
            tip = "Password (not shown while typing"
            tip += ("; Enter = keep)" if old else ")") + ": "
            pw = getpass.getpass("  " + tip)
            if pw or old:
                break
            print("  " + "This is required")
        srv["password"] = pw or old
        for k in ("key_file", "key_passphrase", "password_file", "keychain_service"):
            srv.pop(k, None)
    elif method == "key":
        default_key = srv.get("key_file") or next(
            (f"~/.ssh/{k}" for k in ("id_ed25519", "id_rsa", "id_ecdsa") if (Path.home() / ".ssh" / k).exists()), "")
        srv["key_file"] = ask("  " + "Private key path", default_key,
                              check=lambda v: None if Path(clean_path(v)).is_file() else "File not found")
        old = srv.get("key_passphrase") or ""
        tip = "Key passphrase (press Enter if none"
        tip += ("; Enter = keep)" if old else ")") + ": "
        pp = getpass.getpass("  " + tip)
        srv["key_passphrase"] = pp or old or None
        for k in ("password", "password_file", "keychain_service"):
            srv.pop(k, None)

    step(3, "Which folder, and where to")
    local = ask("Local folder (you can drag the folder in here)",
                syn.get("local_dir", ""), check=check_local)
    syn["local_dir"] = clean_path(local)
    while True:
        remote = ask("Folder on the server (created if missing)",
                     syn.get("remote_dir", ""), check=check_remote)
        remote = remote.rstrip("/") or "/"
        r0 = remote.rstrip("/")
        if re.fullmatch(r"/|/root|/home/[^/]+", remote) and not ask_yes(
                "  " + f"\"{remote}\" is a whole home or root folder; a dedicated subfolder is better (e.g. {r0}/sync). Use it anyway",
                False):
            continue
        syn["remote_dir"] = remote
        break

    step(4, "Sync rules")
    print("  " + "Delete files on the server that were deleted locally? (only files within this task's filter)")
    syn["delete_remote_extra"] = ask_yes("  " + "Delete them too", bool(syn.get("delete_remote_extra", False)))
    syn["excludes"] = ask_list("Files or folders not to sync (by name, * wildcard allowed)",
                               list(syn.get("excludes", DEFAULT_EXCLUDES)), "node_modules .build *.tmp")

    step(5, "Sync only some files (optional; all empty = sync everything)")
    flt["only_suffixes"] = ask_list("Only these suffixes", list(flt.get("only_suffixes", [])), ".zip .pdf")
    flt["only_keywords"] = ask_list("Or only file names containing",
                                    list(flt.get("only_keywords", [])), "contract Gateway")
    flt["skip_suffixes"] = ask_list("Skip these suffixes", list(flt.get("skip_suffixes", [])), ".log .bak")
    flt["skip_keywords"] = ask_list("Skip file names containing",
                                    list(flt.get("skip_keywords", [])), "draft test")
    for k in ("only_suffixes", "skip_suffixes"):
        core.norm_suffixes(flt[k], k)

    step(6, "Scheduled sync")
    sch["interval_minutes"] = ask_int("Scheduled sync interval in minutes (used only when scheduled sync is started)",
                                      int(sch.get("interval_minutes", 10)), 1, 1440)

    if ask_yes("\n" + "Change advanced options (speed limit, compression, connections, upload rule, symlinks, host key, hash method, timeout)", False):
        syn["bwlimit_kbps"] = ask_int("  " + "Upload speed limit in KB/s (0 = unlimited; 1024 KB/s = 1 MB/s)",
                                      int(syn.get("bwlimit_kbps", 0)), 0, 10_000_000)
        if is_ftp:
            srv.pop("compress", None)
        else:
            srv["compress"] = ask_yes("  " + ("Compress the transfer (helps on slow links with text files; "
                                               "usually slower on a LAN or for images, video and archives)"),
                                      bool(srv.get("compress", False)))
        syn["workers"] = ask_int("  " + "Parallel upload connections (FTP on routers and NAS often limits connections; try 1-2)"
                                 if is_ftp else "  " + "Parallel upload connections",
                                 int(syn.get("workers", 2 if is_ftp else 4)), 1, 16)
        if is_ftp:
            srv["encoding"] = ask_choice("  " + "File name encoding on the server (choose GBK if Chinese names break)",
                                         list(core.FTP_ENCODINGS.items()), srv.get("encoding", "auto"))
        else:
            srv.pop("encoding", None)
        syn["upload_when"] = ask_choice("  " + "When to upload again", [
            ("different", "When the modification time or size differs (recommended)"),
            ("newer", "Only when the local file is newer or the size differs (newer server files are kept)")],
            syn.get("upload_when", "different"))
        syn["follow_symlinks"] = ask_yes("  " + "Upload what symbolic links point to (otherwise they are skipped)",
                                         bool(syn.get("follow_symlinks", False)))
        syn["settle_seconds"] = ask_int("  " + "Skip files modified less than this many seconds ago (may still be written)",
                                        int(syn.get("settle_seconds", 2)), 0, 3600)
        if proto != "ftp":
            srv["accept_new_host_key"] = ask_yes(
                "  " + ("Save the FTPS certificate fingerprint on first connection (a changed certificate is refused later)"
                        if proto == "ftps" else
                        "Save the host key automatically on first connection (if off, log in once with ssh first)"),
                bool(srv.get("accept_new_host_key", True)))
        vf["method"] = ask_choice("  " + "Hash method", [
            ("auto", "Auto: let the server compute it (commands, or FTP hash commands) if it can, otherwise download"),
            ("download", "Always download and hash locally (slow; only if the server's own results are unreliable)")],
            vf.get("method", "auto") if vf.get("method") != "ssh" else "auto")
        vf["full_every_hours"] = ask_int(
            "  " + "Hours between full hash checks of local and server files (0 = every round)",
            int(vf.get("full_every_hours", 24)), 0, 720)
        if not is_ftp:
            srv["known_hosts"] = clean_path(ask("  " + "Host key file (known_hosts)",
                                            srv.get("known_hosts", "~/.ssh/known_hosts")))
        srv["timeout"] = ask_int("  " + "Connection timeout (seconds)", int(srv.get("timeout", 30)), 5, 600)
    srv.setdefault("accept_new_host_key", True)
    srv.setdefault("timeout", 30)
    syn.setdefault("workers", 2 if is_ftp else 4)
    vf.setdefault("method", "auto")
    vf.setdefault("full_every_hours", 24)

    new = {k: v for k, v in raw.items() if k not in ("server", "sync", "filter", "schedule", "verify")}
    new.update(server=srv, sync=syn, filter=flt, schedule=sch, verify=vf)
    return new_name, new


def show_settings(name: str, raw: dict) -> None:
    srv, syn, flt = raw["server"], raw["sync"], raw.get("filter", {})
    if srv.get("password"):
        login = "password login"
    elif srv.get("key_file"):
        login = f"key {srv['key_file']}"
    elif srv.get("keychain_service"):
        login = "password from the keychain"
    else:
        login = f"password file {srv.get('password_file', '')}"
    parts = []
    contains = lambda k: f"contains \"{k}\""
    if flt.get("only_suffixes") or flt.get("only_keywords"):
        parts.append("only" + " "
                     + " ".join(flt.get("only_suffixes", []) + [contains(k) for k in flt.get("only_keywords", [])]))
    if flt.get("skip_suffixes") or flt.get("skip_keywords"):
        parts.append("skip" + " "
                     + " ".join(flt.get("skip_suffixes", []) + [contains(k) for k in flt.get("skip_keywords", [])]))
    print()
    with core.table_rows(f"Task {name}"):
        _settings_rows(srv, syn, raw, parts, login)


def _settings_rows(srv: dict, syn: dict, raw: dict, parts: list[str], login: str) -> None:
    proto = srv.get("protocol", "sftp")
    core.row("Connection", core.PROTOCOLS[proto].split(" (", 1)[0])
    core.row("Server", f"{srv['username']}@{srv['host']}:{srv.get('port', core.DEFAULT_PORTS[proto])} ({login})")
    core.row("Local folder", syn["local_dir"])
    core.row("Server folder", syn["remote_dir"])
    core.row("Delete extras", "yes" if syn.get("delete_remote_extra") else "no")
    core.row("Excluded", ", ".join(syn.get("excludes", [])) or "(none)")
    core.row("Filter", "; ".join(parts) or "sync everything")
    core.row("Interval", f"{raw['schedule']['interval_minutes']} min")
    lim = int(syn.get("bwlimit_kbps", 0))
    core.row("Speed limit", f"{lim} KB/s" if lim else "unlimited")
    if proto not in ("ftp", "ftps"):
        core.row("Compression", "on" if srv.get("compress") else "off")
    h = int(raw.get("verify", {}).get("full_every_hours", 24))
    core.row("Full check", f"every {h} h" if h else "every round")


LAST_CONNECTION_CHECKS: list[list[str]] = []  # Rows from the latest connection test, also available in JSON.


def test_connection(raw: dict, interactive: bool = True) -> bool:
    """Check server login and destination access before saving settings."""
    TASKS.mkdir(exist_ok=True)
    tmp = TASKS / ".connection-test.tmp"
    write_task(tmp, raw)
    rows: list[list[str]] = []
    LAST_CONNECTION_CHECKS.clear()

    def done(ok: bool) -> bool:
        LAST_CONNECTION_CHECKS.extend(rows)
        core.print_table(["Check", "Result"], rows,
                         "Connection test")
        return ok

    try:
        cfg, err = load_cfg(tmp)
        if not cfg:
            rows.append(["Settings", f"❌ {err}"])
            return done(False)
        server = f"{cfg.username}@{cfg.host}:{cfg.port} ({cfg.protocol.upper()})"
        rows.append(["Server", server])
        print(f"Connecting to {cfg.host}:{cfg.port} …")
        try:
            try:
                conn = core.connect(cfg)
            except (paramiko.BadHostKeyException, core.CertChanged) as e:
                msg = core.explain_error(e, cfg)
                what = "certificate" if cfg.protocol == "ftps" else "host key"
                if not interactive:
                    rows.append(["Login", "❌ " + msg])
                    return done(False)
                print("  ❌ " + msg.replace("\n", "\n  "))
                if not ask_yes("  " + f"Are you sure the server's {what} really changed (reinstalled, reset), and save the new one",
                               False):
                    rows.append(["Login", "❌ " + f"the server {what} changed"])
                    return done(False)
                core.forget_server_key(cfg)
                conn = core.connect(cfg)
        except Exception as e:
            rows.append(["Login", "❌ " + core.explain_error(e, cfg)])
            return done(False)
        try:
            for host, fp in core.NEW_HOST_KEYS:
                rows.append(["First connection", f"saved host key {fp}"])
            core.NEW_HOST_KEYS.clear()
            rows.append(["Login", "✅ " + "Login OK"])
            d = cfg.remote_dir
            try:
                conn.fs.stat(d)
                rows.append(["Server folder", "✅ " + f"{d} exists"])
            except FileNotFoundError:
                rows.append(["Server folder",
                             "ℹ️ " + f"{d} does not exist yet; it will be created on the first sync"])
            except OSError as e:
                rows.append(["Server folder", "⚠️ " + f"cannot read {d}: {e}"])
        finally:
            conn.close()
        return done(True)
    finally:
        tmp.unlink(missing_ok=True)


def run_cli(path: Path, *args: str) -> int:
    """Run the sync command in a child process."""
    return with_sigint_ignored(lambda: subprocess.call(
        [sys.executable, str(SCRIPT), "-c", str(path), *args], preexec_fn=child_sigint))


def confirm_mass_delete(path: Path, args: list[str]) -> None:
    """Ask for confirmation after the deletion safeguard blocks a round."""
    cfg, _ = load_cfg(path)
    if not cfg:
        return
    try:
        last = json.loads((core.state_dir() / f"{core.state_key(cfg)}.last.json").read_text())
    except (OSError, ValueError):
        return
    n = int(last.get("delete_blocked") or 0)
    if not n:
        return
    print()
    with core.table_rows():
        core.row("⚠️ Note", (f"{n} server files no longer exist locally and would be deleted, "
                                        "but that is above the safety limit."))
        core.row("Local folder", str(cfg.local_dir))
        core.row("Check first",
                 ("the local folder is right and its files are there (is the external drive connected?). "
                   "Deleted server files are gone."))
    if ask_yes("  " + f"Delete these {n} files on the server", False):
        run_cli(path, *args, "--allow-mass-delete")


def edit_task(path: Path | None) -> None:
    """Create or edit a task through the wizard."""
    old_name = path.stem if path else None
    raw = read_raw(path) if path else {}
    old_cfg, _ = load_cfg(path) if path else (None, "")
    was_running = core.read_daemon(old_cfg) if old_cfg else None

    print("\n" + "Enter = keep the value in brackets; press Ctrl+C to cancel at any time.")
    name = old_name
    while True:
        name, raw = wizard(raw, name)
        show_settings(name, raw)
        if ask_yes("\n" + "Test the connection to the server now", True):
            if test_connection(raw):
                break
            print()
            core.print_table(["No.", "Option"],
                             [["1", "Go back and edit"], ["2", "Save anyway"],
                              ["0", "Cancel, don't save"]])
            c = ask("Choose", "1",
                    check=lambda v: None if v in ("0", "1", "2") else "Enter 0, 1 or 2")
            if c == "0":
                print("Cancelled, nothing saved.")
                return
            if c == "2":
                break
            continue
        break

    new_path = TASKS / f"{name}.toml"
    write_task(new_path, raw)
    if path and new_path != path:
        path.unlink(missing_ok=True)
    print("\n✅ " + f"Task \"{name}\" saved")

    # Scheduling
    if was_running:
        core.stop_daemon(old_cfg)
        run_cli(new_path, "--start")
        print("(scheduled sync restarted with the new settings)")
    elif not path and ask_yes("Sync once now", False):
        run_cli(new_path)


def delete_task(path: Path) -> None:
    name = path.stem
    if not ask_yes(f"Delete task \"{name}\"? Only the settings on this computer are removed; files on the server stay",
                   False):
        print("Not deleted.")
        return
    cfg, _ = load_cfg(path)
    if cfg and core.read_daemon(cfg):
        core.stop_daemon(cfg)
    path.unlink(missing_ok=True)
    print("✅ " + f"Task \"{name}\" deleted")


# ---------------------------------------------------------------- Command-line task management

# Map each setting to its TOML section, type, and type-specific options.
SET_KEYS = {
    "protocol": ("server", "choice", list(core.PROTOCOLS)),
    "host": ("server", "str", None),
    "port": ("server", "int", (1, 65535)),
    "username": ("server", "str", None),
    "key_file": ("server", "file", None),
    "password_file": ("server", "file", None),
    "keychain_service": ("server", "str", None),
    "compress": ("server", "bool", None),
    "accept_new_host_key": ("server", "bool", None),
    "timeout": ("server", "int", (5, 600)),
    "known_hosts": ("server", "path", None),
    "encoding": ("server", "choice", list(core.FTP_ENCODINGS)),
    "local_dir": ("sync", "dir", None),
    "remote_dir": ("sync", "remote", None),
    "delete_remote_extra": ("sync", "bool", None),
    "excludes": ("sync", "list", None),
    "workers": ("sync", "int", (1, 16)),
    "bwlimit_kbps": ("sync", "int", (0, 10_000_000)),
    "upload_when": ("sync", "choice", ["different", "newer"]),
    "follow_symlinks": ("sync", "bool", None),
    "settle_seconds": ("sync", "int", (0, 3600)),
    "only_suffixes": ("filter", "list", None),
    "only_keywords": ("filter", "list", None),
    "skip_suffixes": ("filter", "list", None),
    "skip_keywords": ("filter", "list", None),
    "interval_minutes": ("schedule", "int", (1, 1440)),
    "verify_method": ("verify", "choice", ["auto", "download"]),
    "full_every_hours": ("verify", "int", (0, 720)),
}
LOGIN_KEYS = ("password", "key_file", "password_file", "keychain_service")
SECRET_KEYS = ("password", "key_passphrase")
TRUE_WORDS, FALSE_WORDS = ("true", "yes", "y", "1", "on"), ("false", "no", "n", "0", "off")


class SetError(Exception):
    """A supplied task setting is invalid."""


def parse_set_value(key: str, text: str):
    sec, kind, extra = SET_KEYS[key]
    v = text.strip()
    if kind == "int":
        lo, hi = extra
        if not re.fullmatch(r"-?\d+", v) or not lo <= int(v) <= hi:
            raise SetError(f"{key} must be an integer from {lo} to {hi}")
        return int(v)
    if kind == "bool":
        if v.lower() in TRUE_WORDS:
            return True
        if v.lower() in FALSE_WORDS:
            return False
        raise SetError(f"{key} must be true or false")
    if kind == "list":
        return [x for x in re.split(r"[,\uff0c\s]+", v) if x]
    if kind == "choice":
        if v not in extra:
            raise SetError(f"{key} must be one of: {', '.join(extra)}")
        return v
    if kind == "dir":
        err = check_local(v)
        if err:
            raise SetError(err)
        return str(Path(clean_path(v)).resolve())
    if kind == "file":
        p = clean_path(v)
        if not Path(p).expanduser().is_file():
            raise SetError(f"{key}: file not found: {p}")
        return p
    if kind == "path":
        return clean_path(v)
    if kind == "remote":
        err = check_remote(v)
        if err:
            raise SetError(err)
        return v.rstrip("/") or "/"
    if not v:
        raise SetError(f"{key} must not be empty")
    return v


def masked(raw: dict) -> dict:
    """Redact authentication secrets from displayed settings."""
    out = {}
    for sec, body in raw.items():
        out[sec] = {k: ("***" if k in SECRET_KEYS and v else v) for k, v in body.items()} if isinstance(body, dict) else body
    return out


def set_task_cli(name: str, sets: list[str], password: str | None, test: bool) -> tuple[int, dict]:
    """Create or update a named task using command-line settings."""
    res: dict = {"ok": False, "task": name}
    path = TASKS / f"{name}.toml"
    created = not path.exists()
    err = check_name(name, name)
    try:
        if err:
            raise SetError(err)
        raw = {} if created else read_raw(path)
        srv, syn = dict(raw.get("server", {})), dict(raw.get("sync", {}))
        flt, sch, vf = dict(raw.get("filter", {})), dict(raw.get("schedule", {})), dict(raw.get("verify", {}))
        secs = {"server": srv, "sync": syn, "filter": flt, "schedule": sch, "verify": vf}
        old_proto = srv.get("protocol", "sftp")
        changed: dict = {}
        for item in sets:
            if "=" not in item:
                raise SetError(f"Use KEY=VALUE: {item}")
            key, val = item.split("=", 1)
            key = key.strip()
            if key in ("password", "key_passphrase"):
                raise SetError(("Do not put passwords on the command line (they end up in the history and process list); "
                                 "use --password-stdin"))
            if key not in SET_KEYS:
                raise SetError(f"Unknown setting: {key}. Available: {', '.join(SET_KEYS)}")
            value = parse_set_value(key, val)
            sec = SET_KEYS[key][0]
            secs[sec]["method" if key == "verify_method" else key] = value
            changed[key] = value
            if key in LOGIN_KEYS:
                for k in LOGIN_KEYS + ("key_passphrase",):
                    if k != key and not (key == "key_file" and k == "key_passphrase"):
                        srv.pop(k, None)
        if password is not None:
            if not password:
                raise SetError("The password read from stdin is empty")
            srv["password"] = password
            for k in ("key_file", "key_passphrase", "password_file", "keychain_service"):
                srv.pop(k, None)
            changed["password"] = "***"
        if not changed:
            raise SetError("Nothing to change; add --set KEY=VALUE")

        proto = srv.setdefault("protocol", "sftp")
        if "port" not in changed and int(srv.get("port", core.DEFAULT_PORTS[old_proto])) == core.DEFAULT_PORTS[old_proto]:
            srv["port"] = core.DEFAULT_PORTS[proto]  # Recompute the default port after a protocol change.
        is_ftp = proto in ("ftp", "ftps")
        if is_ftp and srv.get("key_file"):
            raise SetError("FTP / FTPS cannot log in with a key file")
        if is_ftp:
            srv.pop("compress", None)
        srv.setdefault("accept_new_host_key", True)
        srv.setdefault("timeout", 30)
        syn.setdefault("delete_remote_extra", False)
        syn.setdefault("excludes", list(DEFAULT_EXCLUDES))
        syn.setdefault("workers", 2 if is_ftp else 4)
        sch.setdefault("interval_minutes", 10)
        vf.setdefault("method", "auto")
        vf.setdefault("full_every_hours", 24)
        for k in ("only_suffixes", "skip_suffixes"):
            if flt.get(k):
                core.norm_suffixes(flt[k], k)
        missing = [k for k, d in (("host", srv), ("username", srv), ("local_dir", syn), ("remote_dir", syn)) if not d.get(k)]
        if not any(srv.get(k) for k in LOGIN_KEYS):
            missing.append("password (--password-stdin) / key_file / password_file / keychain_service")
        if missing:
            raise SetError(f"Missing settings: {', '.join(missing)}")
        warnings = []
        remote = syn["remote_dir"]
        if re.fullmatch(r"/|/root|/home/[^/]+", remote):
            if syn.get("delete_remote_extra"):
                raise SetError((f"\"{remote}\" is a whole home or root folder and delete_remote_extra is on; "
                                 "that is too dangerous. Use a dedicated subfolder"))
            warnings.append(f"\"{remote}\" is a whole home or root folder; a dedicated subfolder is better")
        if proto == "ftp":
            warnings.append("Plain FTP is not encrypted; use it only on your own LAN")

        new = {k: v for k, v in raw.items() if k not in secs}
        new.update({k: v for k, v in secs.items()})
        # Validate a temporary file through the normal loader before replacing the task.
        TASKS.mkdir(exist_ok=True)
        check_path = TASKS / f".{name}.check"  # Keep validation files out of task discovery.
        write_task(check_path, new)
        try:
            core.load_config(check_path)
        except SystemExit as e:
            raise SetError(str(e))
        finally:
            check_path.unlink(missing_ok=True)
        res.update(created=created, changed={k: ("***" if k in SECRET_KEYS else v) for k, v in changed.items()},
                   warnings=warnings, settings=masked(new))
        if test:
            ok = test_connection(new, interactive=False)
            res["connection_ok"] = ok
            res["connection_checks"] = [{"check": c, "result": r} for c, r in LAST_CONNECTION_CHECKS]
            if not ok:
                why = next((r for _, r in LAST_CONNECTION_CHECKS if r.startswith("❌")), "")
                raise SetError("Cannot connect to the server; nothing saved" + (f": {why}" if why else ""))
    except (SetError, ValueError) as e:
        res["error"] = str(e)
        print("❌ " + str(e))
        return 1, res

    old_cfg, _ = (None, "") if created else load_cfg(path)
    was_running = core.read_daemon(old_cfg) if old_cfg else None
    write_task(path, new)
    show_settings(name, new)
    core.print_table(None, [["Result", "✅ " + (f"Task \"{name}\" created" if created
                                                       else f"Task \"{name}\" saved")]]
                     + [["⚠️ " + "Note", w] for w in res["warnings"]])
    res["restarted"] = False
    if was_running:  # Scheduling
        core.stop_daemon(old_cfg)
        _restart_daemon(path)
        res["restarted"] = True
    res["ok"] = True
    return 0, res


def _restart_daemon(path: Path) -> None:
    """Start the schedule with the updated task configuration."""
    sys.stdout.flush()
    with_sigint_ignored(lambda: subprocess.call([sys.executable, str(SCRIPT), "-c", str(path), "--start"],
                                                stdout=sys.stdout, preexec_fn=child_sigint))


def delete_task_cli(name: str, yes: bool) -> tuple[int, dict]:
    """Delete a local task definition after explicit confirmation."""
    res: dict = {"ok": False, "task": name}
    path = TASKS / f"{name}.toml"
    if not path.exists():
        res.update(error=f"No task named \"{name}\"", tasks=[t.stem for t in core.list_tasks()])
    elif not yes:
        res["error"] = "Add --yes to confirm deleting the task (only the settings on this computer; files on the server stay)"
    if "error" in res:
        print("❌ " + res["error"])
        return 1, res
    cfg, _ = load_cfg(path)
    was_running = bool(cfg and core.read_daemon(cfg))
    if was_running:
        core.stop_daemon(cfg)
    path.unlink(missing_ok=True)
    res.update(ok=True, deleted=True, stopped_schedule=was_running, log_file=cfg.log_file if cfg else None)
    rows = [["Result", "✅ " + f"Task \"{name}\" deleted"]]
    if was_running:
        rows.append(["Scheduled sync", "stopped"])
    rows.append(["Note", ("Files on the server and the local folder "
                                   "are untouched; the log file is kept")])
    core.print_table(None, rows)
    return 0, res


def rename_task_cli(old: str, new: str) -> tuple[int, dict]:
    """Rename a task and its default logs, restarting its schedule if necessary."""
    res: dict = {"ok": False, "task": old, "new_name": new}
    path, new_path = TASKS / f"{old}.toml", TASKS / f"{new}.toml"
    if not path.exists():
        res.update(error=f"No task named \"{old}\"", tasks=[t.stem for t in core.list_tasks()])
    elif new == old:
        res["error"] = "The new name is the same as the old one"
    else:
        err = check_name(new, old)
        if err:
            res["error"] = err
    if "error" in res:
        print("❌ " + res["error"])
        return 1, res
    cfg, _ = load_cfg(path)
    was_running = bool(cfg and core.read_daemon(cfg))
    if was_running:
        core.stop_daemon(cfg)
    os.replace(path, new_path)
    raw = read_raw(new_path)
    write_task(new_path, raw)
    moved = []
    if "file" not in raw.get("log", {}):
        old_log, new_log = Path(core.default_log_file(old)), Path(core.default_log_file(new))
        for f in sorted(old_log.parent.glob(old_log.name + "*")):
            suffix = f.name[len(old_log.name):]
            if suffix and not re.fullmatch(r"\.\d+", suffix):
                continue
            dest = new_log.with_name(new_log.name + suffix)
            if not dest.exists():
                os.replace(f, dest)
                moved.append(dest.name)
    if was_running:
        _restart_daemon(new_path)
    res.update(ok=True, task=new, old_name=old, config=str(new_path.resolve()), restarted=was_running, logs_moved=moved)
    rows = [["Result", "✅ " + f"Task \"{old}\" renamed to \"{new}\""]]
    if moved:
        rows.append(["Log", f"renamed to {new}.log"])
    if was_running:
        rows.append(["Scheduled sync", "restarted under the new name"])
    core.print_table(None, rows)
    return 0, res


# ---------------------------------------------------------------- Log viewer

def read_tail(path: Path, n: int) -> tuple[list[str], int]:
    """Return the requested log tail and total line count."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return lines[-n:], len(lines)


def follow_log(path: Path) -> None:
    """Print new log lines until interrupted."""
    print("\n" + "—— Following; new lines appear automatically. Press Ctrl+C to stop ——")
    try:
        pos = path.stat().st_size
    except OSError:
        pos = 0
    try:
        while True:
            time.sleep(0.5)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if size < pos:  # A rotated log must be read from the beginning.
                pos = 0
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print()


def view_log(path: Path) -> None:
    cfg, err = load_cfg(path)
    if not cfg:
        print(f"❗ {err}")
        pause()
        return
    if not cfg.log_file:
        print("Logging is turned off for this task.")
        pause()
        return
    lf = Path(cfg.log_file)
    if not lf.exists():
        print(f"No log yet (there will be one after a sync): {lf}")
        pause()
        return
    n = 40
    while True:
        clear()
        lines, total = read_tail(lf, n)
        print(f"━━━━━━━━ Log {path.stem} {lf} ━━━━━━━━")
        if total > len(lines):
            print(f"({total} lines, showing the last {len(lines)})")
        sys.stdout.write("".join(lines))
        print("-" * 42)
        has_less = shutil.which("less") is not None
        opts = ["Enter = back to menu",
                "m = 200 more lines",
                "f = follow new lines"]
        if has_less:
            opts.append("a = view all in less (q to quit)")
        opts.append("r = refresh")
        v = read_line(", ".join(opts) + ": ").strip().lower()
        if v == "":
            return
        if v == "m":
            n += 200
        elif v == "f":
            follow_log(lf)
            read_line("Press Enter to continue…")
        elif v == "a" and has_less:
            with_sigint_ignored(lambda: subprocess.call(["less", "+G", str(lf)], preexec_fn=child_sigint))


def legacy_config() -> Path | None:
    for n in ("config.toml", "config.example.toml"):
        p = BASE / n
        if not p.exists():
            continue
        try:
            raw = read_raw(p)
        except Exception:
            continue
        host = raw.get("server", {}).get("host", "")
        if host and host != "example.com" and raw.get("sync", {}).get("local_dir"):
            return p
    return None


def import_legacy() -> None:
    if core.list_tasks():
        return
    p = legacy_config()
    if not p:
        return
    raw = read_raw(p)
    srv, syn = raw.get("server", {}), raw.get("sync", {})
    print(f"Found old settings ({p.name}):")
    print(f"  {syn.get('local_dir')} → {srv.get('username', '')}@{srv.get('host')}:{syn.get('remote_dir')}")
    if not ask_yes("Import them as a sync task", True):
        return
    base = Path(os.path.expanduser(str(syn.get("local_dir", "")))).name or "task1"
    default = base if check_name(base, None) is None else "task1"
    name = ask("Task name", default, check=lambda v: check_name(v, None))
    raw.setdefault("schedule", {}).setdefault("interval_minutes", 10)
    new_path = TASKS / f"{name}.toml"
    old_cfg, _ = load_cfg(p)
    write_task(new_path, raw)
    # Keep the legacy configuration as a private backup.
    backup = BASE / "old-config-imported.toml"
    os.replace(p, backup)
    os.chmod(backup, 0o600)
    print("✅ " + f"Imported as task \"{name}\"; the old file was renamed to {backup.name} and can be deleted later")
    d = core.read_daemon(old_cfg) if old_cfg else None
    if d and Path(d.get("config", "")) != new_path.resolve():
        core.stop_daemon(old_cfg)
        run_cli(new_path, "--start")
    pause()


# ---------------------------------------------------------------- Menu

def pick(tasks: list[Path], multi: bool) -> list[Path]:
    if len(tasks) == 1:
        return tasks
    tip = (" (several: separate with spaces, a = all, Enter = back)"
           if multi else " (Enter = back)")
    while True:
        v = read_line("Task number" + tip + ": ").strip().lower()
        if not v:
            return []
        if multi and v in ("a", "all"):
            return tasks
        nums = re.split(r"[,\uff0c\s]+", v)
        if all(n.isdigit() and 1 <= int(n) <= len(tasks) for n in nums) and (multi or len(nums) == 1):
            seen = []
            for n in nums:
                if tasks[int(n) - 1] not in seen:
                    seen.append(tasks[int(n) - 1])
            return seen
        print("  " + "Enter one of the task numbers listed above")


ACTIONS = {
    "1": ("Sync now", []),
    "2": ("Start scheduled sync", ["--start"]),
    "3": ("Stop scheduled sync", ["--stop"]),
    "4": ("Status and last result", ["--status"]),
    "5": ("Dry run: list what would be uploaded, change nothing", ["--dry-run"]),
    "V": ("Full check: sync once and recompute local and server file hashes", ["--full-verify"]),
}
OTHER = [("6", "New task"), ("7", "Edit task"), ("8", "Delete task"), ("9", "View log")]


def print_menu(tasks: list[Path]) -> None:
    print("Cli Sync")
    if tasks:
        running = 0
        for t in tasks:
            cfg, _ = load_cfg(t)
            running += bool(cfg and core.read_daemon(cfg))
        n = len(tasks)
        tasks_en = f"{n} task" + ("s" if n != 1 else "")
        if running:
            summary = f"{tasks_en}, {running} on scheduled sync"
        else:
            summary = f"{tasks_en}, no scheduled sync running"
        core.print_table(["No.", "Task",
                          "Schedule", "Last sync",
                          "Local → server"],
                         [task_row(i, t) for i, t in enumerate(tasks, 1)], summary)
    else:
        print("No sync task yet. Choose 6 to create one.")
    opts = [[key, label] for key, (label, _) in ACTIONS.items()]
    opts += OTHER + [("0", "Quit")]
    core.print_table(["Key", "Action"], opts)
    print()


def menu() -> int:
    try:
        import_legacy()
    except (Back, KeyboardInterrupt):
        print()
    while True:
        clear()
        tasks = core.list_tasks()
        print_menu(tasks)
        try:
            c = read_line("Enter a number and press Enter (Enter alone = refresh)" + ": ").strip()
        except (Back, KeyboardInterrupt):
            print()
            return 0
        c = c.upper()
        if c in ("0", "Q"):
            return 0
        if c == "":
            continue
        try:
            if c in ACTIONS:
                if not tasks:
                    print("No task yet. Choose 6 to create one.")
                    pause()
                    continue
                action, args = ACTIONS[c]
                chosen = pick(tasks, multi=True)
                for t in chosen:
                    if len(chosen) > 1 or len(tasks) > 1:
                        print(f"\n━━━━━━━━ Task {t.stem}: {action} ━━━━━━━━")
                    run_cli(t, *args)
                    if c in ("1", "V"):
                        confirm_mass_delete(t, args)
                if chosen:
                    pause()
            elif c == "6":
                edit_task(None)
                pause()
            elif c in ("7", "8", "9"):
                if not tasks:
                    print("No task yet.")
                    pause()
                    continue
                chosen = pick(tasks, multi=False)
                if not chosen:
                    continue
                if c == "9":
                    view_log(chosen[0])
                else:
                    (edit_task if c == "7" else delete_task)(chosen[0])
                    pause()
            else:
                print(f"No such option: {c}")
                time.sleep(1)
        except (Back, KeyboardInterrupt):
            print("\n" + "Cancelled, back to the menu.")
            time.sleep(1)
