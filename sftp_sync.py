#!/usr/bin/env python3
"""Run one-way folder sync on macOS and Linux over SFTP, SSH, FTPS, or FTP."""

from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import fcntl
import fnmatch
import ftplib
import json
import hashlib
import io
import logging
import logging.handlers
import os
import posixpath
import re
import shlex
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import threading
import time

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Ubuntu 22.04 ships Python 3.10.
    import tomli as tomllib
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

log = logging.getLogger("sftp-sync")

IS_MAC = sys.platform == "darwin"


def cache_dir() -> Path:
    """Return the directory for hash caches, state files, and locks."""
    if IS_MAC:
        d = Path.home() / "Library/Caches/sftp-sync"
    else:
        d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sftp-sync"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    if IS_MAC:
        return Path.home() / "Library/Logs/sftp-sync"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sftp-sync"


def default_log_file(name: str = "") -> str:
    """Return a task log path, or sync.log for a legacy configuration."""
    return str(log_dir() / f"{name or 'sync'}.log")


TASKS_DIR = Path(__file__).resolve().with_name("tasks")


def task_name_of(path: Path) -> str:
    path = Path(path).resolve()
    return path.stem if path.parent == TASKS_DIR else ""

TMP_SUFFIX = ".sftpsync-tmp"
STAGING_DIR = ".sftpsync-staging"
DEFAULT_EXCLUDES = [".DS_Store", "._*", ".Spotlight-V100", ".Trashes", "*.swp", "*~", "*" + TMP_SUFFIX]


# ---------------------------------------------------------------- Cancellation

STOP = threading.Event()  # Set on interruption to cancel pending work.


class Stopped(KeyboardInterrupt):
    """Signal cancellation in worker threads without triggering retries."""


def check_stop() -> None:
    if STOP.is_set():
        raise Stopped()


def pool_run(fn, items: list, workers: int) -> list[tuple[object, BaseException | None]]:
    """Run items concurrently and return ordered results. Cancel pending work on interruption."""
    out: list = [(None, None)] * len(items)
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for fu in as_completed(futs):
            try:
                out[futs[fu]] = (fu.result(), None)
            except Stopped:
                raise
            except Exception as e:
                out[futs[fu]] = (None, e)
    except KeyboardInterrupt:
        STOP.set()
        ex.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        ex.shutdown(wait=True)
    return out


# ---------------------------------------------------------------- Errors

class ConnectFailed(OSError):
    """The connection could not be established."""

    def __init__(self, orig: BaseException):
        super().__init__(str(orig))
        self.orig = orig


class RemoteError(OSError):
    """An operation failed after the server accepted the connection."""


class ScanError(OSError):
    """The source or destination file list could not be used safely."""


# ---------------------------------------------------------------- Configuration

@dataclass
class Config:
    local_dir: Path
    host: str
    remote_dir: str
    port: int = 22
    username: str = ""
    key_file: str | None = None
    key_passphrase: str | None = None
    password: str | None = None
    password_file: str | None = None
    keychain_service: str | None = None
    known_hosts: str = "~/.ssh/known_hosts"
    accept_new_host_key: bool = False
    workers: int = 4
    upload_when: str = "different"  # Compare size and mtime, or size and a newer local mtime.
    delete_remote_extra: bool = False
    follow_symlinks: bool = False
    settle_seconds: int = 2
    excludes: list[str] = field(default_factory=list)
    only_suffixes: list[str] = field(default_factory=list)   # An empty suffix allowlist does not filter files.
    only_keywords: list[str] = field(default_factory=list)   # An empty keyword allowlist does not filter files.
    skip_suffixes: list[str] = field(default_factory=list)
    skip_keywords: list[str] = field(default_factory=list)
    log_file: str | None = None
    timeout: int = 30
    verify_method: str = "auto"  # auto | ssh | download
    full_verify_hours: int = 24  # Hours between full verification rounds. Zero checks every round.
    force_full_verify: bool = False
    compress: bool = False
    bwlimit_kbps: int = 0
    allow_mass_delete: bool = False  # Override the mass-deletion guard for this round only.
    protocol: str = "sftp"  # sftp | ssh | ftps | ftp
    ftp_encoding: str = "auto"
    ftp_detected: str = ""  # Share the detected FTP file name encoding across connections.
    name: str = ""


def _str_list(v, key: str) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise SystemExit(f"[filter] {key} must be a list, e.g. {key} = [\"a\", \"b\"]")
    return [x.strip() for x in v if x.strip()]


def norm_suffixes(v, key: str) -> list[str]:
    # Normalize suffixes like swift, .swift, and *.swift to .swift.
    out = []
    for x in _str_list(v, key):
        x = nfc(x.lstrip("*").lower())
        out.append(x if x.startswith(".") else "." + x)
    return out


def norm_keywords(v, key: str) -> list[str]:
    return [nfc(x.lower()) for x in _str_list(v, key)]


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"The task settings file is malformed ({path}): {e}. Choose 7 in the menu to edit and save the task again")
    try:
        return _load_config(path, raw)
    except (ValueError, TypeError, AttributeError) as e:
        raise SystemExit(f"A value in the task settings is wrong ({path}): {e}. Choose 7 in the menu to edit and save the task again")


def _load_config(path: Path, raw: dict) -> Config:
    s = raw.get("server", {})
    sync = raw.get("sync", {})
    lg = raw.get("log", {})
    flt = raw.get("filter", {})
    vf = raw.get("verify", {})

    def need(d, k, sect):
        if k not in d or d[k] in ("", None):
            raise SystemExit(f"Setting missing: [{sect}] {k}")
        return d[k]

    local_dir = Path(os.path.expanduser(need(sync, "local_dir", "sync"))).resolve()
    if not local_dir.is_dir():
        raise SystemExit(f"Local folder does not exist: {local_dir}")

    name = task_name_of(path)
    protocol = str(s.get("protocol", "sftp")).lower()
    if protocol not in PROTOCOLS:
        raise SystemExit("[server] protocol must be sftp, ssh, ftps or ftp")
    cfg = Config(
        name=name,
        protocol=protocol,
        local_dir=local_dir,
        remote_dir=need(sync, "remote_dir", "sync").rstrip("/") or "/",
        host=need(s, "host", "server"),
        port=int(s.get("port", DEFAULT_PORTS[protocol])),
        username=s.get("username") or os.environ.get("USER", ""),
        key_file=os.path.expanduser(s["key_file"]) if s.get("key_file") else None,
        key_passphrase=s.get("key_passphrase"),
        password=s.get("password"),
        password_file=os.path.expanduser(s["password_file"]) if s.get("password_file") else None,
        keychain_service=s.get("keychain_service"),
        known_hosts=s.get("known_hosts", "~/.ssh/known_hosts"),
        accept_new_host_key=bool(s.get("accept_new_host_key", False)),
        timeout=int(s.get("timeout", 30)),
        workers=max(1, int(sync.get("workers", 4))),
        upload_when=sync.get("upload_when", "different"),
        delete_remote_extra=bool(sync.get("delete_remote_extra", False)),
        follow_symlinks=bool(sync.get("follow_symlinks", False)),
        settle_seconds=int(sync.get("settle_seconds", 2)),
        excludes=DEFAULT_EXCLUDES + list(sync.get("excludes", [])),
        only_suffixes=norm_suffixes(flt.get("only_suffixes", []), "only_suffixes"),
        only_keywords=norm_keywords(flt.get("only_keywords", []), "only_keywords"),
        skip_suffixes=norm_suffixes(flt.get("skip_suffixes", []), "skip_suffixes"),
        skip_keywords=norm_keywords(flt.get("skip_keywords", []), "skip_keywords"),
        log_file=os.path.expanduser(lg.get("file") or default_log_file(name)) if lg.get("file", True) else None,
        verify_method=vf.get("method", "auto"),
        full_verify_hours=max(0, int(vf.get("full_every_hours", 24))),
        compress=bool(s.get("compress", False)),
        bwlimit_kbps=max(0, int(sync.get("bwlimit_kbps", 0))),
        ftp_encoding=str(s.get("encoding", "auto")).lower(),
    )
    if cfg.verify_method not in ("auto", "ssh", "download"):
        raise SystemExit("[verify] method must be auto, ssh or download")
    if cfg.ftp_encoding not in FTP_ENCODINGS:
        raise SystemExit("[server] encoding must be auto, utf-8 or gb18030")
    if cfg.upload_when not in ("newer", "different"):
        raise SystemExit("upload_when must be newer or different")
    # Prefer the environment, inline value, password file, then system credential store.
    cfg.password = os.environ.get("SFTP_SYNC_PASSWORD") or cfg.password
    if not cfg.password and cfg.password_file:
        try:
            cfg.password = Path(cfg.password_file).read_text().strip("\r\n")
        except OSError as e:
            raise SystemExit(f"Cannot read password file {cfg.password_file}: {e}")
    if not cfg.password and cfg.keychain_service:
        cfg.password = read_keychain(cfg.keychain_service, cfg.username)

    if cfg.password == '\u5728\u8fd9\u91cc\u586b\u5bc6\u7801':
        raise SystemExit(f"Please set password in {path} to the server login password")
    if cfg.key_file and not os.path.exists(cfg.key_file):
        if cfg.password:
            cfg.key_file = None  # Allow password login when the configured key is unavailable.
        else:
            raise SystemExit(f"Private key not found: {cfg.key_file}. Change this task's login method in the menu")
    if s.get("password") and os.stat(path).st_mode & 0o077:
        print(f"Note: {path} contains a password and is readable by other users. Run: chmod 600 '{path}'",
              file=sys.stderr)
    return cfg


def read_keychain(service: str, account: str) -> str:
    """Read credentials from macOS Keychain or Linux Secret Service."""
    if IS_MAC:
        cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    else:
        cmd = ["secret-tool", "lookup", "service", service, "account", account]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(("secret-tool not found (sudo apt install libsecret-tools); "
                           "or use password_file / the SFTP_SYNC_PASSWORD environment variable"))
    if r.returncode != 0 or not r.stdout:
        raise SystemExit(f"Not found in keychain: service={service} account={account}: {r.stderr.strip()}")
    return r.stdout.rstrip("\n")


# ---------------------------------------------------------------- Connections

class HostKeyUnknown(paramiko.SSHException):
    """The server key is unknown and automatic enrollment is disabled."""

    def __init__(self, hostname: str, fp: str):
        super().__init__(hostname, fp)
        self.hostname, self.fp = hostname, fp

    def __str__(self):
        return f"The host key {self.fp} of {self.hostname} is not known yet"


# Remember newly trusted fingerprints for the round summary.
NEW_HOST_KEYS: list[tuple[str, str]] = []
_hostkey_lock = threading.Lock()


def fingerprint(key: paramiko.PKey) -> str:
    """Return the host key fingerprint in OpenSSH format."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class AppendHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Append a newly trusted fingerprint without rewriting known_hosts."""

    def __init__(self, path: str):
        self.path = path

    def missing_host_key(self, client, hostname, key):
        line = f"{hostname} {key.get_name()} {key.get_base64()}"
        # Serialize concurrent writes to known_hosts.
        with _hostkey_lock:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            created = not os.path.exists(self.path)
            with open(self.path, "a+b") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                existing = f.read().decode("utf-8", "replace")
                if line not in existing:
                    if existing and not existing.endswith("\n"):
                        f.write(b"\n")
                    f.write((line + "\n").encode())
                    fp = fingerprint(key)
                    NEW_HOST_KEYS.append((hostname, fp))
                    log.warning(f"First connection to {hostname}: saved host key {fp} ({key.get_name()}) to {self.path}")
            if created:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        client.get_host_keys().add(hostname, key.get_name(), key)


class SftpUnavailable(paramiko.SSHException):
    """SSH authentication succeeded, but the server could not open SFTP."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class RejectUnknownHostPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise HostKeyUnknown(hostname, fingerprint(key))


def ssh_connect(cfg: Config) -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    kh = os.path.expanduser(cfg.known_hosts)
    if os.path.exists(kh):
        # Load existing known_hosts entries without rewriting them.
        ssh.load_system_host_keys(kh)
    if cfg.accept_new_host_key:
        ssh.set_missing_host_key_policy(AppendHostKeyPolicy(kh))
    else:
        ssh.set_missing_host_key_policy(RejectUnknownHostPolicy())
    try:
        _ssh_login(ssh, cfg)
    except paramiko.SSHException:
        raise
    except OSError as e:
        raise ConnectFailed(e) from e
    ssh.get_transport().set_keepalive(30)
    return ssh


def _ssh_login(ssh: paramiko.SSHClient, cfg: Config) -> None:
    ssh.connect(
        cfg.host, port=cfg.port, username=cfg.username,
        key_filename=cfg.key_file, passphrase=cfg.key_passphrase,
        password=cfg.password, timeout=cfg.timeout, banner_timeout=cfg.timeout,
        auth_timeout=cfg.timeout,
        look_for_keys=cfg.key_file is None and cfg.password is None,
        allow_agent=cfg.password is None,
        compress=cfg.compress,
    )


# ---------------------------------------------------------------- Remote filesystems

PROTOCOLS = {
    'sftp': 'SFTP (recommended)',
    'ssh': 'SSH commands only (for servers you can ssh into but without SFTP, e.g. routers)',
    'ftps': 'FTPS (encrypted FTP)',
    'ftp': 'FTP (unencrypted: password and files are sent in plain text; use only on your own LAN)',
}
DEFAULT_PORTS = {"sftp": 22, "ssh": 22, "ftps": 21, "ftp": 21}


@dataclass
class RAttr:
    filename: str
    st_mode: int
    st_size: int
    st_mtime: int


class RemoteFS:
    """Shared operations for absolute remote paths. Missing files raise FileNotFoundError."""

    kind = ""
    mtime_exact = True  # Whether remote modification times are precise and writable.
    can_resume = True   # Whether uploads can resume.

    def stat(self, path: str) -> RAttr: raise NotImplementedError
    def lstat(self, path: str) -> RAttr: return self.stat(path)
    def size(self, path: str) -> int: return self.stat(path).st_size
    def listdir_attr(self, path: str) -> list[RAttr]: raise NotImplementedError
    def mkdir(self, path: str, mode: int = 0o777) -> None: raise NotImplementedError
    def remove(self, path: str) -> None: raise NotImplementedError
    def rmdir(self, path: str) -> None: raise NotImplementedError
    def rename(self, src: str, dst: str) -> None: raise NotImplementedError
    def set_attrs(self, path: str, mtime: int, mode: int) -> None: raise NotImplementedError
    def write_file(self, local_path: Path, path: str, offset: int, cb=None, throttle=None) -> None: raise NotImplementedError
    def read_into(self, path: str, cb) -> None: raise NotImplementedError
    def close(self) -> None: pass

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
        except FileNotFoundError:
            return False
        return True

    def remove_if_present(self, path: str) -> None:
        try:
            self.remove(path)
        except FileNotFoundError:
            pass

    def recover_replace(self, dst: str, backup: str) -> None:
        if self.exists(dst):
            return
        try:
            attrs = self.lstat(backup)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(attrs.st_mode):
            raise RemoteError(f"Rollback copy is not a regular file: {backup}")
        self.rename(backup, dst)

    def replace(self, src: str, dst: str, backup: str) -> None:
        """Try the server's rename first, keeping a rollback copy if it cannot overwrite."""
        try:
            self.rename(src, dst)
        except OSError:
            if not self.exists(src) or not self.exists(dst) or self.exists(backup):
                raise
            if not stat.S_ISREG(self.lstat(dst).st_mode):
                raise
            try:
                self.rename(dst, backup)
                self.rename(src, dst)
            except OSError:
                try:
                    self.recover_replace(dst, backup)
                except OSError as e:
                    log.warning("%s %s: %s", "Could not restore old version", backup, e)
                raise
        try:
            self.remove_if_present(backup)
        except OSError as e:
            log.warning("%s %s: %s", "Could not clean up old version", backup, e)


CHUNK = 32768  # Match Paramiko’s transfer chunk size.


class SftpFS(RemoteFS):
    kind = "sftp"

    def __init__(self, sftp: paramiko.SFTPClient):
        self.sftp = sftp

    @staticmethod
    def _attr(a, name: str) -> RAttr:
        return RAttr(name, a.st_mode or 0, a.st_size or 0, int(a.st_mtime or 0))

    def stat(self, path):
        return self._attr(self.sftp.stat(path), posixpath.basename(path))

    def lstat(self, path):
        return self._attr(self.sftp.lstat(path), posixpath.basename(path))

    def listdir_attr(self, path):
        return [self._attr(a, a.filename) for a in self.sftp.listdir_attr(path)]

    def mkdir(self, path, mode=0o777):
        self.sftp.mkdir(path, mode)

    def remove(self, path):
        self.sftp.remove(path)

    def rmdir(self, path):
        self.sftp.rmdir(path)

    def rename(self, src, dst):
        try:
            self.sftp.posix_rename(src, dst)
        except (OSError, IOError):
            self.sftp.rename(src, dst)

    def set_attrs(self, path, mtime, mode):
        self.sftp.utime(path, (mtime, mtime))
        try:
            self.sftp.chmod(path, mode)
        except OSError:
            pass

    def write_file(self, local_path, path, offset, cb=None, throttle=None):
        sent = offset
        with open(local_path, "rb") as fl, self.sftp.open(path, "r+b" if offset else "wb") as fr:
            fr.set_pipelined(True)
            if offset:
                fl.seek(offset)
                fr.seek(offset)
            while data := fl.read(CHUNK):
                check_stop()
                if throttle:
                    throttle.consume(len(data))
                fr.write(data)
                sent += len(data)
                if cb:
                    cb(sent - offset, 0)
        check_size(self.size(path), sent)

    def read_into(self, path, cb):
        with self.sftp.open(path, "rb") as f:
            f.prefetch()
            while chunk := f.read(1 << 20):
                cb(chunk)

    def close(self):
        self.sftp.close()


def check_size(size: int, sent: int) -> None:
    if size != sent:
        raise RemoteError(f"size on server {size} != sent {sent} (disk full?)")


def detect_stat(ssh: paramiko.SSHClient) -> str | None:
    """Identify the GNU, BusyBox, or BSD implementation of stat."""
    for kind, cmd in (("gnu", "stat --printf '%i' /"), ("busybox", "stat -c '%i' /"), ("bsd", "stat -f '%i' /")):
        try:
            rc, out = ssh_exec(ssh, cmd + " 2>/dev/null")
        except (paramiko.SSHException, OSError, EOFError):
            return None  # SFTP-only accounts cannot run commands.
        if rc == 0 and out.strip().isdigit():
            return kind
    return None


class ShellFS(RemoteFS):
    """Transfer and manage files with basic commands over SSH."""

    kind = "ssh"

    def __init__(self, ssh: paramiko.SSHClient, stat_kind: str, timeout: int):
        self.ssh, self.timeout = ssh, timeout
        self.stat_cmd = "stat -f '%Xp %z %m %N'" if stat_kind == "bsd" else "stat -c '%f %s %Y %n'"

    def run(self, script: str, data: bytes = b"") -> tuple[int, bytes]:
        # Use sh regardless of the account’s login shell.
        return ssh_exec(self.ssh, "sh -c " + shlex.quote(script), data)

    def _parse(self, line: bytes) -> RAttr | None:
        parts = line.split(b" ", 3)
        if len(parts) != 4:
            return None
        try:
            mode, size, mtime = int(parts[0], 16), int(parts[1]), int(parts[2])
        except ValueError:
            return None
        return RAttr(posixpath.basename(parts[3].decode("utf-8", "surrogateescape")), mode, size, mtime)

    def stat(self, path):
        rc, out = self.run(f"{self.stat_cmd} -- {shlex.quote(path)} 2>/dev/null")
        a = self._parse(out.rstrip(b"\n")) if rc == 0 else None
        if a is None:
            raise FileNotFoundError(path)
        return a

    def size(self, path):
        rc, out = self.run(f"wc -c < {shlex.quote(path)} 2>/dev/null")
        if rc != 0 or not out.strip().isdigit():
            raise FileNotFoundError(path)
        return int(out.strip())

    def listdir_attr(self, path):
        # List a whole directory with stat, ignoring unmatched globs.
        rc, out = self.run(f"cd {shlex.quote(path)} || exit 3; {self.stat_cmd} -- * .[!.]* ..?* 2>/dev/null; exit 0")
        if rc == 3:
            raise FileNotFoundError(path)
        return [a for a in map(self._parse, out.split(b"\n")) if a]

    def _check(self, script: str, what: str, path: str) -> None:
        rc, _ = self.run(script)
        if rc == 127:
            raise RemoteError(f"The server has no {what} command, so \"SSH commands only\" does not work")
        if rc != 0:
            raise RemoteError(f"{what} {path}: exit {rc}")

    def mkdir(self, path, mode=0o777):
        prefix = "umask 077; " if mode == 0o700 else ""
        self._check(f"{prefix}mkdir {shlex.quote(path)}", "mkdir", path)

    def remove(self, path):
        q = shlex.quote(path)
        rc, _ = self.run(f"[ -e {q} ] || [ -L {q} ] || exit 2; rm -f {q}")
        if rc == 2:
            raise FileNotFoundError(path)
        if rc:
            raise RemoteError(f"rm {path}: exit {rc}")

    def rmdir(self, path):
        self._check(f"rmdir {shlex.quote(path)}", "rmdir", path)

    def rename(self, src, dst):
        qdst = shlex.quote(dst)
        self._check(f"[ ! -d {qdst} ] && mv -f {shlex.quote(src)} {qdst}", "mv", src)

    def set_attrs(self, path, mtime, mode):
        stamp = time.strftime("%Y%m%d%H%M.%S", time.gmtime(mtime))
        q = shlex.quote(path)
        self._check(f"TZ=UTC0 touch -c -t {stamp} {q} && {{ chmod {mode:o} {q} 2>/dev/null; true; }}", "touch", path)

    def write_file(self, local_path, path, offset, cb=None, throttle=None):
        chan = self.ssh.get_transport().open_session()
        chan.settimeout(self.timeout * 4)
        chan.exec_command("sh -c " + shlex.quote(f"cat {'>>' if offset else '>'} {shlex.quote(path)}"))
        sent = offset
        try:
            with open(local_path, "rb") as fl:
                fl.seek(offset)
                while data := fl.read(CHUNK):
                    check_stop()
                    if throttle:
                        throttle.consume(len(data))
                    chan.sendall(data)
                    sent += len(data)
                    if cb:
                        cb(sent - offset, 0)
            chan.shutdown_write()
            rc = chan.recv_exit_status()
        finally:
            chan.close()
        if rc:
            raise RemoteError(f"cat > {path}: exit {rc}")
        check_size(self.size(path), sent)

    def read_into(self, path, cb):
        chan = self.ssh.get_transport().open_session()
        chan.settimeout(3600)
        chan.exec_command("sh -c " + shlex.quote(f"cat {shlex.quote(path)}"))
        try:
            while data := chan.recv(1 << 20):
                cb(data)
            rc = chan.recv_exit_status()
        finally:
            chan.close()
        if rc:
            raise FileNotFoundError(path)


class CertUnknown(Exception):
    """The FTPS certificate is unknown and automatic enrollment is disabled."""

    def __init__(self, hostname: str, fp: str):
        super().__init__(hostname, fp)
        self.hostname, self.fp = hostname, fp


class CertChanged(Exception):
    def __init__(self, hostname: str, expected: str, got: str):
        super().__init__(hostname, expected, got)
        self.hostname, self.expected, self.got = hostname, expected, got


def forget_server_key(cfg: Config) -> None:
    """Remove a previous fingerprint after the operator confirms a server key change."""
    if cfg.protocol == "ftps":
        path, key = known_certs_file(), f"{cfg.host}:{cfg.port}"
        with _hostkey_lock:
            lines = path.read_text().splitlines() if path.exists() else []
            path.write_text("".join(l + "\n" for l in lines if l.split(" ", 1)[0] != key))
        return
    name = cfg.host if cfg.port == 22 else f"[{cfg.host}]:{cfg.port}"
    subprocess.run(["ssh-keygen", "-R", name, "-f", os.path.expanduser(cfg.known_hosts)],
                   capture_output=True, check=False)


def known_certs_file() -> Path:
    return Path.home() / ".config/sftp-sync/known_certs"


def check_cert(cfg: Config, der: bytes) -> None:
    """Pin the FTPS certificate fingerprint to support servers with self-signed certificates."""
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")
    key = f"{cfg.host}:{cfg.port}"
    path = known_certs_file()
    with _hostkey_lock:
        known = {}
        try:
            for line in path.read_text().splitlines():
                k, _, v = line.strip().partition(" ")
                if k and v:
                    known[k] = v
        except FileNotFoundError:
            pass
        if key in known:
            if known[key] != fp:
                raise CertChanged(key, known[key], fp)
            return
        if not cfg.accept_new_host_key:
            raise CertUnknown(key, fp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{key} {fp}\n")
        NEW_HOST_KEYS.append((key, fp))
        log.warning(f"First connection to {key}: saved the FTPS certificate fingerprint {fp} to {path}")


class _FTP_TLS(ftplib.FTP_TLS):
    """Reuse the control session when the server requires it for data connections."""

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
                control_cert = self.sock.getpeercert(binary_form=True)
                if not conn.session_reused and (not control_cert or conn.getpeercert(binary_form=True) != control_cert):
                    raise ssl.SSLError("FTPS data connection certificate differs from the control connection")
            except BaseException:
                conn.close()
                self.close()  # Discard the unfinished response on the control connection.
                raise
        return conn, size


MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun",
                                      "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
UNIX_LIST = re.compile(r"^([\-dlcbps])[rwxsStT\-]{9}\S*\s+\d+\s+\S+(?:\s+\S+)?\s+(\d+)\s+(\w{3})\s+(\d{1,2})\s+"
                       r"(\d{1,2}:\d{2}|\d{4})\s(.+)$")
DOS_LIST = re.compile(r"^(\d{2})-(\d{2})-(\d{2,4})\s+(\d{1,2}):(\d{2})\s*([AP]M)\s+(<DIR>|\d+)\s+(.+)$", re.I)


def parse_list_line(line: str) -> RAttr | None:
    """Parse a Unix or DOS FTP LIST entry with approximate modification time."""
    m = UNIX_LIST.match(line)
    if m:
        t, size, mon, day, tm, name = m.groups()
        mode = stat.S_IFDIR | 0o755 if t == "d" else stat.S_IFREG | 0o644 if t == "-" else 0
        mon_n = MONTHS.get(mon.lower(), 1)
        if ":" in tm:
            hh, mm = map(int, tm.split(":"))
            year = time.gmtime().tm_year
            ts = calendar.timegm((year, mon_n, int(day), hh, mm, 0))
            if ts > time.time() + 86400 * 2:  # Treat an inferred future date as belonging to the previous year.
                ts = calendar.timegm((year - 1, mon_n, int(day), hh, mm, 0))
        else:
            ts = calendar.timegm((int(tm), mon_n, int(day), 0, 0, 0))
        return RAttr(name, mode, int(size), ts)
    m = DOS_LIST.match(line)
    if m:
        mo, d, y, hh, mm, ap, size, name = m.groups()
        y = int(y) + (2000 if len(y) == 2 else 0)
        hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
        ts = calendar.timegm((y, int(mo), int(d), hh, int(mm), 0))
        if size.upper() == "<DIR>":
            return RAttr(name, stat.S_IFDIR | 0o755, 0, ts)
        return RAttr(name, stat.S_IFREG | 0o644, int(size), ts)
    return None


def ftp_time(v: str) -> int:
    v = v.split(".")[0]
    return calendar.timegm(time.strptime(v, "%Y%m%d%H%M%S"))


class _Reader:
    """Update transfer progress and enforce the rate limit while ftplib reads a file."""

    def __init__(self, f, throttle, cb):
        self.f, self.throttle, self.cb, self.n = f, throttle, cb, 0

    def read(self, size=-1):
        check_stop()
        data = self.f.read(size)
        if data:
            if self.throttle:
                self.throttle.consume(len(data))
            self.n += len(data)
            if self.cb:
                self.cb(self.n, 0)
        return data


HASH_ALGOS = {"SHA-256": "sha256", "SHA-1": "sha1", "MD5": "md5"}


FTP_ENCODINGS = {
    'auto': 'auto-detect',
    'utf-8': 'UTF-8',
    'gb18030': 'GBK (Chinese names on older devices and Windows)',
}


class FtpFS(RemoteFS):
    """Run passive FTP or explicit FTPS operations, with reconnects and file name decoding."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.kind = cfg.protocol
        self.timeout = cfg.timeout
        if cfg.ftp_encoding != "auto":
            self.enc, self.enc_decided = cfg.ftp_encoding, True
        elif cfg.ftp_detected:  # Reuse the encoding detected by another connection.
            self.enc, self.enc_decided = cfg.ftp_detected, True
        else:
            self.enc, self.enc_decided = "utf-8", False
        self.list_a: bool | None = None  # Cache whether the server supports LIST -a.
        self.hash_cmd: str | None = None
        self.hash_algo: str | None = None
        self._open(first=True)

    # ---- Connections
    def _open(self, first: bool = False) -> None:
        cfg = self.cfg
        tls = cfg.protocol == "ftps"
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # Use the pinned fingerprint checked by check_cert.
            ftp = _FTP_TLS(context=ctx, timeout=cfg.timeout)
        else:
            ftp = ftplib.FTP(timeout=cfg.timeout)
        ftp.encoding = "latin-1"
        try:
            ftp.connect(cfg.host, cfg.port)
        except OSError as e:
            raise ConnectFailed(e) from e
        if tls:
            ftp.auth()
            check_cert(cfg, ftp.sock.getpeercert(binary_form=True))
        ftp.login(self._p(cfg.username), self._p(cfg.password or ""))
        if tls:
            ftp.prot_p()
        ftp.set_pasv(True)
        self.ftp = ftp
        if first:
            self.feats: dict[str, str] = {}
            try:
                for line in ftp.sendcmd("FEAT").splitlines()[1:-1]:
                    name, _, rest = line.strip().partition(" ")
                    self.feats[name.upper()] = rest
            except ftplib.all_errors:
                pass
            if "UTF8" in self.feats and self.cfg.ftp_encoding == "auto":
                self.enc, self.enc_decided = "utf-8", True
                self.cfg.ftp_detected = "utf-8"
            self.mlsd = "MLST" in self.feats
            self.mfmt = "MFMT" in self.feats
            self.mtime_exact = self.mlsd and self.mfmt
            self.can_resume = "REST" in self.feats
        if self.enc == "utf-8" and "UTF8" in self.feats:
            try:
                ftp.sendcmd("OPTS UTF8 ON")
            except ftplib.all_errors:
                pass
        ftp.voidcmd("TYPE I")
        if first:
            self._detect_hash()
        elif self.hash_cmd == "HASH":
            try:
                ftp.sendcmd(f"OPTS HASH {[k for k, v in HASH_ALGOS.items() if v == self.hash_algo][0]}")
            except ftplib.all_errors:
                pass

    def _detect_hash(self) -> None:
        # Prefer server-side hashing to downloading file contents.
        if "HASH" in self.feats:
            names = [a.strip().rstrip("*").upper() for a in self.feats["HASH"].split(";")]
            for want in ("SHA-256", "SHA-1", "MD5"):
                if want in names:
                    try:
                        self.ftp.sendcmd(f"OPTS HASH {want}")
                        self.hash_cmd, self.hash_algo = "HASH", HASH_ALGOS[want]
                    except ftplib.all_errors:
                        continue
                    break
        if not self.hash_cmd:
            for cmd, algo in (("XSHA256", "sha256"), ("XSHA1", "sha1"), ("XMD5", "md5")):
                if cmd in self.feats or self._probe(cmd):
                    self.hash_cmd, self.hash_algo = cmd, algo
                    break

    def _probe(self, cmd: str) -> bool:
        """Probe for an unadvertised FTP hash command using an absent path."""
        try:
            self.ftp.sendcmd(f"{cmd} /.sftpsync-probe-{os.getpid()}")
        except ftplib.error_perm as e:
            return str(e)[:3] in ("550", "450", "553")
        except ftplib.all_errors:
            return False
        return False

    @staticmethod
    def _lost(e: BaseException) -> bool:
        """Report whether the failure represents a lost connection."""
        if isinstance(e, ftplib.error_temp):
            return str(e)[:3] == "421"
        return isinstance(e, (EOFError, ConnectionError, TimeoutError, ssl.SSLError, BrokenPipeError))

    def _do(self, fn):
        """Reconnect and retry an FTP operation once after a connection failure."""
        if self.ftp.sock is None:
            self._reconnect("connection closed")
        try:
            return fn()
        except Exception as e:
            if not self._lost(e):
                raise
            self._reconnect(e)
            return fn()

    def _reconnect(self, why) -> None:
        log.info(f"FTP connection lost ({why}), reconnecting")
        try:
            self.ftp.close()
        except Exception:
            pass
        self._open()

    # ---- File name encoding
    def _p(self, path: str) -> str:
        """Encode an FTP path for the Latin-1 byte-preserving transport."""
        return path.encode(self.enc, "surrogateescape").decode("latin-1")

    def _n(self, name: str) -> str:
        """Decode a file name received through the Latin-1 FTP transport."""
        return name.encode("latin-1").decode(self.enc, "surrogateescape")

    def _decide_encoding(self, raw_names: list[str]) -> None:
        """Infer the file name encoding from a non-ASCII entry if UTF-8 was not advertised."""
        if self.enc_decided:
            return
        for n in raw_names:
            b = n.encode("latin-1")
            if b.isascii():
                continue
            try:
                b.decode("utf-8")
            except UnicodeDecodeError:
                self.enc = "gb18030"
                log.info("Server file names are not UTF-8; using GBK")
            self.enc_decided = True
            self.cfg.ftp_detected = self.enc
            return

    def _err(self, e: Exception, path: str):
        if isinstance(e, ftplib.error_perm) and str(e)[:3] in ("550", "450", "553"):
            return FileNotFoundError(f"{path}: {e}")
        if isinstance(e, ftplib.Error):
            return RemoteError(f"{path}: {e}")
        return e if isinstance(e, OSError) else OSError(f"{path}: {e}")

    # ---- File operations
    def _mlst_attr(self, facts: dict, name: str) -> RAttr | None:
        t = facts.get("type", "").lower()
        if t in ("cdir", "pdir"):
            return None
        mode = stat.S_IFDIR | 0o755 if t == "dir" else stat.S_IFREG | 0o644 if t == "file" else 0
        if mode and facts.get("unix.mode"):
            try:
                mode = stat.S_IFMT(mode) | int(facts["unix.mode"], 8)
            except ValueError:
                pass
        mtime = ftp_time(facts["modify"]) if facts.get("modify") else 0
        return RAttr(name, mode, int(facts.get("size", 0) or 0), mtime)

    def stat(self, path):
        if self.mlsd:
            try:
                resp = self._do(lambda: self.ftp.sendcmd(f"MLST {self._p(path)}"))
            except ftplib.all_errors as e:
                raise self._err(e, path)
            for line in resp.splitlines()[1:-1]:
                facts_s, _, _ = line.strip().partition(" ")
                facts = dict(f.split("=", 1) for f in facts_s.split(";") if "=" in f)
                facts = {k.lower(): v for k, v in facts.items()}
                a = self._mlst_attr(facts, posixpath.basename(path))
                if a:
                    return a
                return RAttr(posixpath.basename(path), stat.S_IFDIR | 0o755, 0, 0)
            raise FileNotFoundError(path)
        # Use parent directory entries to match the metadata returned during scanning.
        parent, name = posixpath.split(path.rstrip("/"))
        if not name:
            return RAttr("/", stat.S_IFDIR | 0o755, 0, 0)
        for a in self.listdir_attr(parent or "/"):
            if a.filename == name:
                return a
        raise FileNotFoundError(path)

    def size(self, path):
        try:
            n = self._do(lambda: self.ftp.size(self._p(path)))
        except ftplib.all_errors as e:
            raise self._err(e, path)
        if n is None:
            raise FileNotFoundError(path)
        return n

    def listdir_attr(self, path):
        out: list[RAttr] = []
        try:
            if self.mlsd:
                items = self._do(lambda: list(self.ftp.mlsd(self._p(path), facts=["type", "size", "modify", "unix.mode"])))
                self._decide_encoding([n for n, _ in items])
                for name, facts in items:
                    a = self._mlst_attr({k.lower(): v for k, v in facts.items()}, self._n(name))
                    if a:
                        out.append(a)
                return out
            lines = self._do(lambda: self._list(path, self.list_a is True))
            if self.list_a is None:
                # Compare LIST -a with plain LIST before trusting its hidden-file output.
                try:
                    with_a = self._do(lambda: self._list(path, True))
                except ftplib.error_perm:
                    with_a = None
                plain = {a.filename for a in map(parse_list_line, lines) if a}
                got = {a.filename for a in map(parse_list_line, with_a or []) if a}
                self.list_a = bool(with_a) and plain <= got
                if self.list_a:
                    lines = with_a
        except ftplib.all_errors as e:
            raise self._err(e, path)
        attrs = [a for a in map(parse_list_line, lines) if a and a.filename not in (".", "..")]
        self._decide_encoding([a.filename for a in attrs])
        for a in attrs:
            a.filename = self._n(a.filename)
            out.append(a)
        return out

    def _list(self, path: str, dash_a: bool) -> list[str]:
        lines: list[str] = []
        p = self._p(path)
        self.ftp.retrlines(f"LIST -a {p}" if dash_a else f"LIST {p}", lines.append)
        return lines

    def mkdir(self, path, mode=0o777):
        try:
            self._do(lambda: self.ftp.mkd(self._p(path)))
        except ftplib.all_errors as e:
            raise RemoteError(f"{path}: {e}")
        if mode == 0o700:
            try:
                self._do(lambda: self.ftp.sendcmd(f"SITE CHMOD 700 {self._p(path)}"))
            except ftplib.all_errors:
                pass  # FTP servers may manage directory permissions themselves

    def remove(self, path):
        try:
            self._do(lambda: self.ftp.delete(self._p(path)))
        except ftplib.all_errors as e:
            raise self._err(e, path)

    def rmdir(self, path):
        try:
            self._do(lambda: self.ftp.rmd(self._p(path)))
        except ftplib.all_errors as e:
            raise RemoteError(f"{path}: {e}")

    def rename(self, src, dst):
        try:
            self._do(lambda: self.ftp.rename(self._p(src), self._p(dst)))
        except ftplib.all_errors as e:
            raise RemoteError(f"{src} → {dst}: {e}")

    def set_attrs(self, path, mtime, mode):
        if self.mfmt:
            stamp = time.strftime('%Y%m%d%H%M%S', time.gmtime(mtime))
            try:
                self._do(lambda: self.ftp.sendcmd(f"MFMT {stamp} {self._p(path)}"))
            except ftplib.all_errors as e:
                raise RemoteError(f"MFMT {path}: {e}")
        try:
            self._do(lambda: self.ftp.sendcmd(f"SITE CHMOD {mode:o} {self._p(path)}"))
        except ftplib.all_errors:
            pass  # Permissions are optional on FTP servers.

    def write_file(self, local_path, path, offset, cb=None, throttle=None):
        # The upload retry owns reconnection and resumption.
        with open(local_path, "rb") as fl:
            fl.seek(offset)
            r = _Reader(fl, throttle, cb)
            try:
                self.ftp.storbinary(f"STOR {self._p(path)}", r, blocksize=CHUNK, rest=offset or None)
            except ftplib.all_errors as e:
                raise RemoteError(f"STOR {path}: {e}")
        check_size(self.size(path), offset + r.n)

    def read_into(self, path, cb):
        try:
            self.ftp.retrbinary(f"RETR {self._p(path)}", cb, blocksize=1 << 16)
        except ftplib.all_errors as e:
            raise self._err(e, path)

    def remote_hash(self, path: str) -> str | None:
        """Read the server hash, returning None when the hash is unavailable."""
        def run():
            # Allow more time for hashing large files on slow servers.
            self.ftp.sock.settimeout(1800)
            try:
                return self.ftp.sendcmd(f"{self.hash_cmd} {self._p(path)}")
            finally:
                if self.ftp.sock:
                    self.ftp.sock.settimeout(self.timeout)
        try:
            resp = self._do(run)
        except ftplib.error_perm:
            return None
        except ftplib.all_errors as e:
            raise RemoteError(f"{self.hash_cmd} {path}: {e}")
        n = {"sha256": 64, "sha1": 40, "md5": 32}[self.hash_algo]
        for tok in resp.split()[1:]:
            if len(tok) == n and all(c in "0123456789abcdefABCDEF" for c in tok):
                return tok.lower()
        return None

    def close(self):
        try:
            self.ftp.quit()
        except ftplib.all_errors:
            self.ftp.close()


@dataclass
class Conn:
    fs: RemoteFS
    ssh: paramiko.SSHClient | None = None

    def close(self):
        try:
            self.fs.close()
        finally:
            if self.ssh:
                self.ssh.close()


def connect(cfg: Config) -> Conn:
    if cfg.protocol in ("ftp", "ftps"):
        return Conn(FtpFS(cfg))
    ssh = ssh_connect(cfg)
    try:
        if cfg.protocol == "ssh":
            kind = detect_stat(ssh)
            if kind is None:
                raise ShellUnavailable()
            return Conn(ShellFS(ssh, kind, cfg.timeout), ssh)
        try:
            sftp = ssh.open_sftp()
        except Exception as e:
            raise SftpUnavailable(str(e) or type(e).__name__) from e
        sftp.get_channel().settimeout(cfg.timeout * 4)
        return Conn(SftpFS(sftp), ssh)
    except BaseException:
        ssh.close()
        raise


class ShellUnavailable(paramiko.SSHException):
    """Required filesystem commands are unavailable over SSH."""


class ConnPool:
    """Give each transfer or verification worker its own connection."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.local = threading.local()
        self.all: list[Conn] = []
        self.lock = threading.Lock()

    def get_connection(self) -> Conn:
        conn = getattr(self.local, "conn", None)
        if conn is None or not conn_alive(conn):
            conn = connect(self.cfg)
            self.local.conn = conn
            with self.lock:
                self.all.append(conn)
        return conn

    def get(self) -> RemoteFS:
        return self.get_connection().fs

    def reset(self):
        self.local.conn = None

    def close(self):
        for c in self.all:
            try:
                c.close()
            except Exception:
                pass
        self.all.clear()


def conn_alive(conn: Conn) -> bool:
    if conn.ssh is not None:
        t = conn.ssh.get_transport()
        return bool(t and t.is_active())
    return getattr(conn.fs, "ftp", None) is not None and conn.fs.ftp.sock is not None


# ---------------------------------------------------------------- Scanning

@dataclass
class Entry:
    size: int
    mtime: int


def is_excluded(rel: str, patterns: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)
               for pattern in map(nfc, patterns))


def file_selected(rel: str, cfg: Config) -> bool:
    """Apply case-insensitive name filters to a relative path."""
    name = rel.rsplit("/", 1)[-1].lower()
    if any(name.endswith(x) for x in cfg.skip_suffixes) or any(k in name for k in cfg.skip_keywords):
        return False
    if not (cfg.only_suffixes or cfg.only_keywords):
        return True
    # A suffix or keyword match is sufficient.
    return any(name.endswith(x) for x in cfg.only_suffixes) or any(k in name for k in cfg.only_keywords)


def filter_desc(cfg: Config) -> str:
    parts = []
    if cfg.only_suffixes or cfg.only_keywords:
        only = [" ".join(cfg.only_suffixes)] if cfg.only_suffixes else []
        if cfg.only_keywords:
            only.append("name contains " + ", ".join(cfg.only_keywords))
        parts.append("only " + " or ".join(only))
    if cfg.skip_suffixes or cfg.skip_keywords:
        skip = [" ".join(cfg.skip_suffixes)] if cfg.skip_suffixes else []
        if cfg.skip_keywords:
            skip.append("name contains " + ", ".join(cfg.skip_keywords))
        parts.append("skip " + " and ".join(skip))
    return "; ".join(parts)


def has_filter(cfg: Config) -> bool:
    return bool(cfg.only_suffixes or cfg.only_keywords or cfg.skip_suffixes or cfg.skip_keywords)


def ancestors(rels) -> set[str]:
    """Return the parent directory and all its ancestors for each path."""
    out: set[str] = set()
    for r in rels:
        d = posixpath.dirname(r)
        while d and d not in out:
            out.add(d)
            d = posixpath.dirname(d)
    return out


def nfc(s: str) -> str:
    # Compare normalized names across macOS and Linux.
    return unicodedata.normalize("NFC", s)


def scan_local(cfg: Config) -> tuple[dict[str, Entry], dict[str, Path], set[str]]:
    files: dict[str, Entry] = {}
    paths: dict[str, Path] = {}
    dirs: set[str] = set()
    root = cfg.local_dir
    seen: dict[str, str] = {}

    def scan_error(error: OSError) -> None:
        raise ScanError(f"Local scan failed: {error}") from error

    for dirpath, dirnames, filenames in os.walk(root, followlinks=cfg.follow_symlinks, onerror=scan_error):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else nfc(rel_dir.replace(os.sep, "/"))
        keep = []
        for d in dirnames:
            rel = f"{rel_dir}/{nfc(d)}" if rel_dir else nfc(d)
            if d != STAGING_DIR and not d.endswith(TMP_SUFFIX) and not is_excluded(rel, cfg.excludes):
                if not cfg.follow_symlinks and (Path(dirpath) / d).is_symlink():
                    continue
                remember_path(seen, rel, str(Path(dirpath) / d))
                keep.append(d)
                dirs.add(rel)
        dirnames[:] = keep
        for fn in filenames:
            rel = f"{rel_dir}/{nfc(fn)}" if rel_dir else nfc(fn)
            if (fn == STAGING_DIR or fn.endswith(TMP_SUFFIX)
                    or is_excluded(rel, cfg.excludes) or not file_selected(rel, cfg)):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat() if cfg.follow_symlinks else p.lstat()
            except FileNotFoundError:
                if cfg.follow_symlinks and p.is_symlink():
                    continue
                raise ScanError(f"Local file vanished during scan: {p}") from None
            except OSError as e:
                scan_error(e)
            if stat.S_ISDIR(st.st_mode):
                raise ScanError(f"Unexpected directory during local scan: {p}")
            if not stat.S_ISREG(st.st_mode):
                continue
            remember_path(seen, rel, str(p))
            files[rel] = Entry(st.st_size, int(st.st_mtime))
            paths[rel] = p
    if has_filter(cfg):
        # Only create directories needed by selected files.
        dirs = ancestors(files)
    return files, paths, dirs


def remember_path(paths: dict[str, str], rel: str, actual: str) -> None:
    previous = paths.setdefault(rel, actual)
    if previous != actual:
        raise ScanError(f"Paths collide after Unicode normalization: {previous!r}, {actual!r}")


def remote_rel(paths: dict[str, str], rel: str) -> str:
    """Use the server's spelling for existing entries and their descendants."""
    if rel in paths:
        return paths[rel]
    parent, name = posixpath.split(rel)
    return posixpath.join(remote_rel(paths, parent), name) if parent else name


def scan_remote(
    fs: RemoteFS, cfg: Config, only_dirs: set[str] | None = None,
) -> tuple[dict[str, Entry], set[str], dict[str, str]]:
    """Only descend into matching local directories when only_dirs is given."""
    files: dict[str, Entry] = {}
    dirs: set[str] = set()
    paths: dict[str, str] = {}
    root = cfg.remote_dir
    try:
        fs.stat(root)
    except FileNotFoundError:
        return files, dirs, paths

    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full = posixpath.join(root, rel_dir) if rel_dir else root
        try:
            items = fs.listdir_attr(full)
        except PermissionError as e:
            raise PermissionError(13, "no permission to read this folder", full) from e
        for a in items:
            if a.filename in (".", ".."):
                continue
            if not a.filename or "/" in a.filename or "\0" in a.filename:
                raise RemoteError("Invalid filename in remote directory listing")
            if a.filename == STAGING_DIR or a.filename.endswith(TMP_SUFFIX):
                continue
            actual = f"{rel_dir}/{a.filename}" if rel_dir else a.filename
            rel = nfc(actual)
            remember_path(paths, rel, actual)
            if stat.S_ISDIR(a.st_mode or 0):
                if not is_excluded(rel, cfg.excludes):
                    dirs.add(rel)
                    if only_dirs is None or rel in only_dirs:
                        stack.append(actual)
            elif stat.S_ISREG(a.st_mode or 0):
                # Exclude these paths from verification and deletion.
                if is_excluded(rel, cfg.excludes) or not file_selected(rel, cfg):
                    continue
                files[rel] = Entry(a.st_size or 0, int(a.st_mtime or 0))
    return files, dirs, paths


# ---------------------------------------------------------------- Synchronization

def needs_upload(cfg: Config, loc: Entry, rem: Entry | None) -> str | None:
    if rem is None:
        return "new"
    if loc.size != rem.size:
        return "size differs"
    # Allow one second of timestamp rounding on FAT-family filesystems.
    if cfg.upload_when == "newer" and loc.mtime > rem.mtime + MTIME_SLACK:
        return "local is newer"
    if cfg.upload_when == "different" and abs(loc.mtime - rem.mtime) > MTIME_SLACK:
        return "mtime differs"
    return None


MTIME_SLACK = 1


def ensure_dirs(fs: RemoteFS, cfg: Config, rels: set[str], existing: set[str], dry: bool,
                paths: dict[str, str]):
    # Create parent directories before their children.
    todo = set()
    for rel in rels:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            todo.add("/".join(parts[:i]))
    word = "mkdir"
    root_parts = cfg.remote_dir.strip("/").split("/")
    for i in range(1, len(root_parts) + 1):
        p = "/" + "/".join(root_parts[:i])
        try:
            fs.stat(p)
        except FileNotFoundError:
            log.info("%s %s", word, p)
            if not dry:
                fs.mkdir(p)
    for rel in sorted(todo - existing, key=lambda x: x.count("/")):
        log.info("%s %s", word, rel)
        if not dry:
            path = posixpath.join(cfg.remote_dir, remote_rel(paths, rel))
            try:
                fs.mkdir(path)
            except OSError:
                if not stat.S_ISDIR(fs.lstat(path).st_mode):
                    raise


class Throttle:
    """Share one rate limit across concurrent uploads."""

    def __init__(self, rate: float):
        self.rate = rate
        self.lock = threading.Lock()
        self.next = time.monotonic()

    def consume(self, n: int) -> None:
        with self.lock:
            now = time.monotonic()
            # Limit burst capacity to half a second of uploads.
            self.next = max(self.next, now - 0.5) + n / self.rate
            wait = self.next - now
        while wait > 0:
            check_stop()
            time.sleep(min(wait, 0.2))
            wait -= 0.2


def staging_paths(fs: RemoteFS, remote: str, task_key: str) -> tuple[str, str]:
    parent, name = posixpath.split(remote)
    directory = posixpath.join(parent, STAGING_DIR)
    try:
        attrs = fs.lstat(directory)
    except FileNotFoundError:
        try:
            fs.mkdir(directory, mode=0o700)
        except OSError:
            if not fs.exists(directory):
                raise
        attrs = fs.lstat(directory)
    if not stat.S_ISDIR(attrs.st_mode):
        raise RemoteError(f"Staging path is not a directory: {directory}")
    key = hashlib.sha256(f"{task_key}\0{nfc(name)}".encode("utf-8", "surrogateescape")).hexdigest()
    return posixpath.join(directory, key + ".part"), posixpath.join(directory, key + ".old")


def upload_one(pool: ConnPool, cfg: Config, rel: str, local_path: Path, prog: "Progress | None" = None,
               throttle: Throttle | None = None, hasher: "RemoteHasher | None" = None):
    remote = posixpath.join(cfg.remote_dir, remote_rel(hasher.paths if hasher else {}, rel))
    for attempt in range(3):
        try:
            conn = pool.get_connection()
            fs = conn.fs
            st = local_path.stat()
            tmp, backup = staging_paths(fs, remote, state_key(cfg))
            fs.recover_replace(remote, backup)
            local_hash = hash_file(local_path)
            offset = 0
            try:
                attrs = fs.lstat(tmp)
                if not stat.S_ISREG(attrs.st_mode):
                    raise RemoteError(f"Staged upload is not a regular file: {tmp}")
                have = fs.size(tmp)
                if have <= st.st_size and fs.can_resume:
                    offset = have  # Verify all resumed content before publication.
                else:
                    fs.remove(tmp)
            except FileNotFoundError:
                pass
            if offset:
                log.info((f"Resuming {rel}: {fmt_bytes(offset)} already on the server, "
                           f"sending the remaining {fmt_bytes(st.st_size - offset)}"))
                if prog:
                    prog.skip(offset)
            if not offset or offset < st.st_size:
                fs.write_file(local_path, tmp, offset, prog.callback(rel) if prog else None, throttle)
            check_size(fs.size(tmp), st.st_size)
            staged_hash = hasher.hash_staged(conn, tmp) if hasher else hash_remote_file(fs, tmp)
            if staged_hash != local_hash or file_signature(local_path.stat()) != file_signature(st):
                fs.remove_if_present(tmp)
                raise RemoteError(f"Staged file verification failed or local file changed: {rel}")
            fs.set_attrs(tmp, int(st.st_mtime), stat.S_IMODE(st.st_mode))
            fs.replace(tmp, remote, backup)
            if prog:
                prog.finish(rel)
            return
        except FileNotFoundError:
            if prog:
                prog.discard(rel)
            raise
        except (paramiko.SSHException, EOFError, OSError, TimeoutError, ftplib.Error) as e:
            if prog:
                prog.discard(rel)  # Count bytes from the resumed offset on the next attempt.
            log.warning(f"Upload of {rel} failed (attempt {attempt + 1}): {e}")
            pool.reset()
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("failed after 3 attempts")


def upload_batch(cfg: Config, rels: list[str], local_paths: dict[str, Path],
                 st: "Stats | None" = None, hasher: "RemoteHasher | None" = None) -> tuple[set[str], bool]:
    """Upload files concurrently and return the successful paths and overall status."""
    pool = ConnPool(cfg)
    done: set[str] = set()
    ok = True
    prog = Progress(cfg, rels, local_paths)
    throttle = Throttle(cfg.bwlimit_kbps * 1024) if cfg.bwlimit_kbps else None
    prog.start()
    try:
        res = pool_run(lambda rel: upload_one(pool, cfg, rel, local_paths[rel], prog, throttle, hasher),
                       rels, cfg.workers)
        for rel, (_, err) in zip(rels, res):
            if err is None:
                done.add(rel)
            else:
                ok = False
                log.error("%s %s: %s", "upload failed", rel, err)
    finally:
        prog.stop()
        pool.close()
    secs = max(time.time() - prog.t0, 0.001)
    sent, speed = fmt_bytes(prog.sent), fmt_bytes(prog.sent / secs)
    log.info(f"Upload done {len(done)}/{len(rels)}, {sent}, average {speed}/s, {secs:.1f} s")
    if st is not None:
        st.upload_bytes += prog.sent
        st.upload_seconds += secs
    return done, ok


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_eta(sec: float) -> str:
    sec = int(sec)
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def text_width(t: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)


def fit_width(t: str, width: int) -> str:
    out, w = "", 0
    for c in t:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > width - 1:
            return out + "…"
        out, w = out + c, w + cw
    return out


PROGRESS_ACTIVE = threading.Event()  # Clear the active progress line before logging.


class Progress:
    """Update transfer progress for the terminal, scheduled status, and logs."""

    def __init__(self, cfg: Config, rels: list[str], local_paths: dict[str, Path]):
        self.total = 0
        for r in rels:
            try:
                self.total += local_paths[r].stat().st_size
            except OSError:
                pass
        self.nfiles = len(rels)
        self.done = 0
        self.sent = 0
        self.active: dict[str, int] = {}  # Bytes transferred for each active upload.
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.samples: list[tuple[float, int]] = [(self.t0, 0)]
        self.tty = sys.stdout.isatty()
        self.path = state_dir() / f"{state_key(cfg)}.progress.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log = self.t0

    def callback(self, rel: str):
        def cb(transferred: int, _total: int):
            with self.lock:
                self.sent += transferred - self.active.get(rel, 0)
                self.active[rel] = transferred
        return cb

    def finish(self, rel: str):
        with self.lock:
            self.active.pop(rel, None)
            self.done += 1

    def discard(self, rel: str):
        with self.lock:
            self.sent -= self.active.pop(rel, 0)

    def skip(self, n: int):
        """Count bytes already present on the server when resuming."""
        with self.lock:
            self.total = max(0, self.total - n)

    def line(self) -> str:
        now = time.time()
        with self.lock:
            sent, done, current = self.sent, self.done, list(self.active)
        # Estimate speed from the last three seconds.
        self.samples.append((now, sent))
        while len(self.samples) > 2 and now - self.samples[0][0] > 3:
            self.samples.pop(0)
        t_old, b_old = self.samples[0]
        speed = max(0.0, (sent - b_old) / (now - t_old)) if now > t_old else 0.0
        pct = sent * 100 / self.total if self.total else 100
        left = fmt_eta((self.total - sent) / speed) if speed > 0 and sent < self.total else "--:--"
        t = (f"{'Uploading'} {done}/{self.nfiles}  {fmt_bytes(sent)} / {fmt_bytes(self.total)}"
             f"  {pct:.0f}%  {fmt_bytes(speed)}/s  {'ETA'} {left}")
        if current:
            name = current[0].rsplit("/", 1)[-1]
            t += f"  {name}" + (f" +{len(current) - 1}"
                                if len(current) > 1 else "")
        return t

    def tick(self):
        t = self.line()
        if self.tty:
            width = shutil.get_terminal_size((100, 20)).columns
            sys.stdout.write("\r\033[K  " + fit_width(t, width - 2))
            sys.stdout.flush()
        else:
            if time.time() - self._last_log >= 30:
                self._last_log = time.time()
                log.info(t)
        try:
            self.path.write_text(json.dumps({"time": time.time(), "line": t}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _run(self):
        while not self._stop.wait(1):
            self.tick()

    def start(self):
        if self.tty:
            PROGRESS_ACTIVE.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        if self.tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            PROGRESS_ACTIVE.clear()
        try:
            self.path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- Verification

def state_dir() -> Path:
    return cache_dir()


def state_key(cfg: Config) -> str:
    return hashlib.sha1(f"{cfg.local_dir}|{cfg.host}|{cfg.remote_dir}".encode()).hexdigest()[:12]


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def tree_hash(hashes: dict[str, str]) -> str:
    """Hash the ordered path and digest pairs for a directory."""
    h = hashlib.sha256()
    for rel in sorted(hashes):
        h.update(rel.encode() + b"\0" + hashes[rel].encode() + b"\n")
    return h.hexdigest()


ALGO_NAMES = {"sha256": "SHA-256", "sha1": "SHA-1", "md5": "MD5"}


def hash_file(p: Path, algo: str = "sha256") -> str:
    h = hashlib.new(algo)
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            check_stop()
            h.update(chunk)
    return h.hexdigest()


def hash_remote_file(fs: RemoteFS, path: str, algo: str = "sha256") -> str:
    digest = hashlib.new(algo)

    def update(chunk: bytes) -> None:
        check_stop()
        digest.update(chunk)

    fs.read_into(path, update)
    return digest.hexdigest()


def file_signature(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


class LocalHashCache:
    """Cache hashes by file identity and metadata, with one cache per algorithm."""

    def __init__(self, cfg: Config, algo: str = "sha256"):
        self.algo = algo
        suffix = "" if algo == "sha256" else f"-{algo}"
        self.path = state_dir() / f"{state_key(cfg)}.hashes{suffix}.json"
        try:
            self.data: dict[str, list] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def get(self, rel: str, p: Path, force: bool = False) -> str | None:
        try:
            signature = file_signature(p.stat())
            c = self.data.get(rel)
            if not force and isinstance(c, list) and len(c) == 2 and c[0] == signature:
                return c[1]
            h = hash_file(p, self.algo)
            after = file_signature(p.stat())
        except FileNotFoundError:
            return None
        if after != signature:
            return None  # Retry a file that changed during hashing next round.
        self.data[rel] = [signature, h]
        return h

    def hash_many(self, items: dict[str, Path], force: bool = False) -> dict[str, str]:
        res = pool_run(lambda kv: self.get(*kv, force=force), list(items.items()), min(8, os.cpu_count() or 4))
        hashes = {}
        for rel, (digest, error) in zip(items, res):
            if digest:
                hashes[rel] = digest
            elif error:
                log.warning("%s %s: %s", "Could not hash local file", rel, error)
        return hashes

    def save(self, keep: set[str] | None = None):
        if keep is not None:
            self.data = {k: v for k, v in self.data.items() if k in keep}
        save_json(self.path, self.data)


def ssh_exec(ssh: paramiko.SSHClient, cmd: str, stdin_data: bytes = b"") -> tuple[int, bytes]:
    chan = ssh.get_transport().open_session()
    chan.settimeout(3600)
    chan.exec_command(cmd)

    def writer():
        try:
            if stdin_data:
                chan.sendall(stdin_data)
        finally:
            chan.shutdown_write()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    chan.settimeout(1)
    out = bytearray()
    deadline = time.time() + 3600
    try:
        while True:
            check_stop()
            try:
                data = chan.recv(1 << 16)
            except TimeoutError:
                if time.time() > deadline:
                    raise
                continue
            if not data:
                break
            out += data
        rc = chan.recv_exit_status()
    finally:
        t.join(5)
        chan.close()
    return rc, bytes(out)


# Server hash commands in preference order.
HASH_TOOLS = [("sha256sum", "sha256"), ("shasum -a 256", "sha256"), ("sha1sum", "sha1"), ("md5sum", "md5")]


class RemoteHasher:
    """Hash remote files with a server command when available, or download them."""

    def __init__(self, cfg: Config, conn: Conn, paths: dict[str, str] | None = None):
        self.cfg, self.conn, self.ssh = cfg, conn, conn.ssh
        self.paths = paths or {}
        self.tool: str | None = None
        self.ftp_hash = False
        self.algo = "sha256"
        auto = cfg.verify_method in ("auto", "ssh")
        if auto and self.ssh is not None:
            found = self._detect()
            if found:
                self.tool, self.algo = found
        if auto and not self.tool and isinstance(conn.fs, FtpFS) and conn.fs.hash_cmd:
            self.ftp_hash, self.algo = True, conn.fs.hash_algo
        if cfg.verify_method == "ssh" and not (self.tool or self.ftp_hash):
            raise RuntimeError("The server cannot compute hashes; choose \"download\" in the task's advanced options")
        name = ALGO_NAMES[self.algo]
        if self.tool:
            self.method = f"on the server ({self.tool})"
        elif self.ftp_hash:
            self.method = f"on the FTP server ({conn.fs.hash_cmd}, {name})"
        else:
            self.method = "downloaded and hashed locally"
        self.stat_kind = detect_stat(self.ssh) if self.ssh is not None else None

    def hash_staged(self, conn: Conn, path: str) -> str:
        """Verify staged content with SHA-256 before making it visible at its final path."""
        if self.algo == "sha256":
            if self.tool and conn.ssh is not None:
                rc, output = ssh_exec(conn.ssh, f"{self.tool} {shlex.quote(path)} 2>/dev/null")
                digest = parse_sum_output(output).get(path)
                if rc == 0 and digest and len(digest) == 64:
                    return digest
            elif self.ftp_hash and isinstance(conn.fs, FtpFS) and conn.fs.hash_algo == "sha256":
                digest = conn.fs.remote_hash(path)
                if digest:
                    return digest
        return hash_remote_file(conn.fs, path)

    def _detect(self) -> tuple[str, str] | None:
        x = b"x"
        for tool, algo in HASH_TOOLS:
            try:
                rc, out = ssh_exec(self.ssh, f"printf x | {tool} 2>/dev/null")
            except (paramiko.SSHException, OSError, EOFError):
                return None  # SFTP-only accounts cannot run commands.
            if rc == 0 and out.startswith(hashlib.new(algo, x).hexdigest().encode()):
                return tool, algo
        return None

    def signatures(self, rels: list[str], scanned: dict[str, Entry], fs: RemoteFS) -> dict[str, str]:
        """Read remote metadata, using scan results for files untouched by this round."""
        if not rels:
            return {}
        if self.stat_kind:
            actual = {remote_rel(self.paths, rel): rel for rel in rels}
            if self.stat_kind == "gnu":
                opt, fmt, sep = "--printf", "%i %Z %s %Y %n\\0", b"\0"
            elif self.stat_kind == "busybox":
                opt, fmt, sep = "-c", "%i %Z %s %Y %n", b"\n"
            else:
                opt, fmt, sep = "-f", "%i %c %z %m %N", b"\n"
            res = {}
            for batch in arg_batches(list(actual)):  # Keep each batch within the command limit.
                cmd = (f"cd {shlex.quote(self.cfg.remote_dir)} && stat {opt} {shlex.quote(fmt)} "
                       + " ".join(shlex.quote("./" + r) for r in batch) + " 2>/dev/null")
                _, out = ssh_exec(self.ssh, cmd)
                for item in out.split(sep):
                    parts = item.split(b" ", 4)
                    if len(parts) == 5 and parts[4].startswith(b"./"):
                        name = parts[4][2:].decode("utf-8", "surrogateescape")
                        if name in actual:
                            res[actual[name]] = "stat:" + ":".join(p.decode() for p in parts[:4])
            return res
        res = {}
        for r in rels:
            e = scanned.get(r)
            if e is None:
                try:
                    a = fs.stat(posixpath.join(self.cfg.remote_dir, remote_rel(self.paths, r)))
                except FileNotFoundError:
                    continue
                e = Entry(a.st_size, a.st_mtime)
            res[r] = f"attr:{e.size}:{e.mtime}"
        return res

    def hashes(self, rels: list[str], sizes: dict[str, int] | None = None) -> tuple[dict[str, str], set[str]]:
        """Return file digests and the paths that could not be verified."""
        if not rels:
            return {}, set()
        if self.tool:
            # Distribute files across workers by size.
            # Leave room for other channels under the SSH session limit.
            n = max(1, min(self.cfg.workers, len(rels), 6))
            groups: list[list[str]] = [[] for _ in range(n)]
            load = [0] * n
            for r in sorted(rels, key=lambda r: -(sizes or {}).get(r, 0)):
                i = load.index(min(load))
                groups[i].append(r)
                load[i] += (sizes or {}).get(r, 0) + 1
            res: dict[str, str] = {}
            retry: list[str] = []
            for grp, (part, err) in zip(groups, pool_run(self._via_ssh, groups, n)):
                if err is not None:
                    log.warning(f"Hashing on the server failed; downloading these {len(grp)} files instead: {err}")
                    retry += grp
                    continue
                got, failed = part
                res.update(got)
                retry += failed
            if retry:
                got, failed = self._via_pool(retry)
                res.update(got)
                return res, failed
            return res, set()
        return self._via_pool(rels)

    def _via_ssh(self, rels: list[str]) -> tuple[dict[str, str], list[str]]:
        """Hash batches over SSH, returning any paths that need a download fallback."""
        res: dict[str, str] = {}
        failed: list[str] = []
        actual = {remote_rel(self.paths, rel): rel for rel in rels}
        for batch in arg_batches(list(actual)):
            cmd = (f"cd {shlex.quote(self.cfg.remote_dir)} && {self.tool} "
                   + " ".join(shlex.quote("./" + r) for r in batch) + " 2>/dev/null")
            _, out = ssh_exec(self.ssh, cmd)
            got = parse_sum_output(out)
            for name in batch:
                if name in got:
                    res[actual[name]] = got[name]
                else:
                    failed.append(actual[name])
        return res, failed

    def _via_pool(self, rels: list[str]) -> tuple[dict[str, str], set[str]]:
        """Hash with parallel connections, retrying failed reads on a fresh connection."""
        pool = ConnPool(self.cfg)
        missing = object()

        def attempt(rel):
            fs = pool.get()
            path = posixpath.join(self.cfg.remote_dir, remote_rel(self.paths, rel))
            if self.ftp_hash:
                got = fs.remote_hash(path)
                if got:
                    return got
                # Fall back to downloading when the hash response cannot be parsed.
            try:
                return hash_remote_file(fs, path, self.algo)
            except FileNotFoundError:
                if rel in self.paths:
                    raise  # A listed file that cannot be read must be reported as unverified.
                return missing

        def one(rel):
            try:
                return attempt(rel)
            except (OSError, EOFError, paramiko.SSHException, ftplib.Error) as e:
                log.warning(f"Reading {rel} on the server failed, retrying: {e}")
                pool.reset()
                return attempt(rel)

        try:
            out = pool_run(one, rels, self.cfg.workers)
        finally:
            pool.close()
        res: dict[str, str] = {}
        failed: set[str] = set()
        for rel, (h, err) in zip(rels, out):
            if err is not None:
                why = str(err).strip().rstrip(":")
                if not why or why.endswith(rel):  # Treat a path-only error as a likely closed connection.
                    why = type(err).__name__ + ": the server closed the connection"
                log.warning(f"Could not verify {rel}: {why}")
                failed.add(rel)
            elif h is not missing and h:
                res[rel] = h
        return res, failed


def arg_batches(rels: list[str], limit: int = 30000) -> list[list[str]]:
    """Split paths into batches below the remote command length limit."""
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for r in rels:
        n = len(shlex.quote("./" + r).encode()) + 1
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


def parse_sum_output(out: bytes) -> dict[str, str]:
    """Parse path and digest pairs from a checksum command."""
    res: dict[str, str] = {}
    for line in out.split(b"\n"):
        if not line:
            continue
        escaped = line.startswith(b"\\")  # Checksum tools escape newlines and backslashes in paths.
        if escaped:
            line = line[1:]
        h, _, name = line.partition(b" ")
        if name[:1] in (b" ", b"*"):
            name = name[1:]
        s = name.decode("utf-8", "surrogateescape")
        if escaped:
            s = s.replace("\\\\", "\0").replace("\\n", "\n").replace("\0", "\\")
        if s.startswith("./"):
            s = s[2:]
        if h and all(c in b"0123456789abcdef" for c in h):
            res[s] = h.decode()
    return res


class RemoteHashCache:
    """Cache matching remote digests by verification algorithm and file signature."""

    def __init__(self, cfg: Config, algo: str = "sha256"):
        self.algo = algo
        self.path = state_dir() / f"{state_key(cfg)}.remote-hashes.json"
        self.full_at, self.files = 0.0, {}
        try:
            d = json.loads(self.path.read_text())
            if d.get("algo", "sha256") == algo:
                self.full_at = float(d.get("full_at", 0))
                self.files: dict[str, list] = dict(d.get("files", {}))
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def save(self, keep: set[str]):
        self.files = {k: v for k, v in self.files.items() if k in keep}
        save_json(self.path, {"algo": self.algo, "full_at": self.full_at, "files": self.files})


def verify_and_repair(cfg: Config, conn: Conn, hasher: RemoteHasher, targets: list[str],
                      local_paths: dict[str, Path], repair: bool, st: Stats | None = None,
                      scanned: dict[str, Entry] | None = None, fresh: set[str] | None = None,
                      sizes: dict[str, int] | None = None) -> bool:
    """Compare both sides and recheck mismatches after uploading them again."""
    cache = LocalHashCache(cfg, hasher.algo)
    rcache = RemoteHashCache(cfg, hasher.algo)
    fresh = set(fresh or ())
    scanned = scanned or {}
    t0 = time.time()
    hours = cfg.full_verify_hours
    full = cfg.force_full_verify or hours == 0 or t0 - rcache.full_at >= hours * 3600
    an = ALGO_NAMES[hasher.algo]
    kind = f"{an} check"
    vr = VerifyResult(kind=kind, method=hasher.method, full=full)
    if st is not None:
        st.verify = vr
    all_lh: dict[str, str] = {}
    all_rh: dict[str, str] = {}
    first_lh: dict[str, str] = {}
    unverified: set[str] = set()
    complete_scope = set(targets) == set(local_paths)
    full_done = False
    try:
        for attempt in range(2):
            lh = cache.hash_many({r: local_paths[r] for r in targets}, force=full)
            names = list(lh)
            sigs = hasher.signatures(names, {r: e for r, e in scanned.items() if r not in fresh}, conn.fs)
            reuse: dict[str, str] = {}
            if not full and attempt == 0:
                for r in names:
                    c = rcache.files.get(r)
                    if r not in fresh and c and sigs.get(r) == c[0] and c[1] == lh[r]:
                        reuse[r] = c[1]
            to_hash = [r for r in names if r not in reuse]
            computed, failed = hasher.hashes(to_hash, sizes)
            unverified.difference_update(targets)
            unverified.update((set(targets) - set(lh)) | failed)
            vr.unverified = sorted(unverified)
            for rel in targets:
                all_lh.pop(rel, None)
                all_rh.pop(rel, None)
                if rel in unverified:
                    rcache.files.pop(rel, None)
            lh = {r: h for r, h in lh.items() if r not in failed}  # Keep read failures out of the mismatch list.
            for r in to_hash:
                if r in failed:
                    continue
                if computed.get(r) == lh[r] and sigs.get(r):
                    rcache.files[r] = [sigs[r], lh[r]]
                else:
                    rcache.files.pop(r, None)
            rh = {**reuse, **computed}
            if attempt == 0:
                vr.hashed, vr.reused = len(to_hash) - len(failed), len(reuse)
            all_lh.update(lh)
            all_rh.update(rh)
            for r in lh:
                if r not in rh:
                    all_rh.pop(r, None)
            bad = sorted(r for r in lh if rh.get(r) != lh[r])
            if attempt == 0:
                first_lh = dict(lh)
            else:
                vr.repaired = sorted(r for r in targets if r in lh and lh[r] == rh.get(r))
            vr.files = len(all_lh)
            # Report tree hashes after attempted repair.
            lt = tree_hash(all_lh)
            rt = tree_hash({r: all_rh[r] for r in all_lh if r in all_rh})
            vr.local_tree, vr.remote_tree = lt, rt
            if attempt == 0 and not unverified:
                comparison = "match" if lt == rt else "MISMATCH"
                log.info((f"{kind}: {len(lh)} files ({len(to_hash) - len(failed)} hashed on the server, {len(reuse)} unchanged since "
                           f"the last check), local tree hash {lt[:16]}, server tree hash {rt[:16]} → {comparison}"))
            if not bad and not unverified:
                full_done = full and complete_scope
                secs = time.time() - t0
                if attempt == 0:
                    log.info(f"{kind} passed: {len(lh)} files ({hasher.method}, {secs:.1f} s)")
                else:
                    log.info(f"{kind}: consistent after re-upload, {len(lh)} files ({hasher.method}, {secs:.1f} s)")
                return True
            if not bad:
                break
            for r in bad[:50]:
                log.warning("%s %s (%s %s, %s %s)", "hash mismatch", r,
                            "local", lh[r][:12], "server",
                            (rh.get(r) or "missing")[:12])
            if len(bad) > 50:
                log.warning(f"… and {len(bad) - 50} more mismatches")
            if not repair or attempt == 1:
                break
            log.warning(f"Re-uploading {len(bad)} mismatched files")
            repaired, _ = upload_batch(cfg, bad, local_paths, st, hasher)
            failed_uploads = set(bad) - repaired
            if st is not None:
                st.upload_failed = sorted(set(st.upload_failed) | failed_uploads)
            unverified.update(failed_uploads)
            vr.unverified = sorted(unverified)
            if not repaired:
                break
            fresh |= repaired
            targets = sorted(repaired)
        # Identify files that changed locally during the round.
        changed = [r for r in bad if r in first_lh and lh.get(r) != first_lh[r]] if attempt == 1 else []
        vr.changed = changed
        vr.bad_final = [r for r in bad if r not in set(changed)]
        if changed:
            log.warning(f"{len(changed)} files changed locally during the sync; they will be uploaded next time")
        if vr.bad_final:
            log.error(f"{kind} failed: {len(vr.bad_final)} files differ from local")
        return False
    finally:
        if repair:
            cache.save(set(local_paths))
            if full_done:
                rcache.full_at = t0
            rcache.save(set(local_paths))
        if hours:
            vr.next_full = rcache.full_at + hours * 3600


# ---------------------------------------------------------------- Results

@dataclass
class VerifyResult:
    kind: str
    files: int = 0
    method: str = ""
    local_tree: str = ""
    remote_tree: str = ""
    repaired: list[str] = field(default_factory=list)
    bad_final: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # Synchronization
    unverified: list[str] = field(default_factory=list)
    full: bool = False
    hashed: int = 0
    reused: int = 0
    next_full: float = 0.0


@dataclass
class Stats:
    local_files: int = 0
    remote_files: int = 0
    uploaded: dict[str, str] = field(default_factory=dict)
    upload_failed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    delete_blocked: int = 0  # Number of deletions blocked by the safeguard.
    delete_failed: list[str] = field(default_factory=list)
    busy: int = 0
    upload_bytes: int = 0
    upload_seconds: float = 0.0
    conflicts: list[str] = field(default_factory=list)
    verify: VerifyResult | None = None
    error: str = ""
    dry: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not (self.error or self.upload_failed or self.delete_failed or self.conflicts or self.delete_blocked
                    or (self.verify and (self.verify.bad_final or self.verify.changed or self.verify.unverified)))

    @property
    def status(self) -> str:
        return "fail" if self.error else "ok" if self.ok else "warn"


def _wrap_cell(text: str, width: int) -> list[str]:
    """Wrap a cell by display width while preserving existing line breaks."""
    out: list[str] = []
    for para in str(text).split("\n"):
        line, w = "", 0
        for c in para:
            cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
            if w + cw > width and line:
                cut = line.rfind(" ")  # Wrap at a space when possible.
                if cut >= len(line) // 3 and c != " " and line[cut + 1:].isascii():
                    out.append(line[:cut])
                    line = line[cut + 1:]
                else:
                    out.append(line)
                    line = ""
                w = text_width(line)
            if c == " " and not line and out:
                continue
            line, w = line + c, w + cw
        out.append(line)
    return out


def print_table(headers: list[str] | None, rows: list[list[str]], title: str = "", compact: bool = False) -> None:
    """Render a terminal table, narrowing the widest columns when needed."""
    if not rows and not headers:
        return
    cols = len(headers) if headers else max(len(r) for r in rows)
    rows = [[str(c) for c in r] + [""] * (cols - len(r)) for r in rows]
    every = ([headers] if headers else []) + rows
    widths = [max(1, max(text_width(ln) for r in every for ln in r[i].split("\n"))) for i in range(cols)]
    budget = max(cols * 6, shutil.get_terminal_size((100, 24)).columns - 1 - (3 * cols + 1))
    while sum(widths) > budget:
        i = max(range(cols), key=lambda k: widths[k])
        if widths[i] <= 6:
            break
        widths[i] -= 1

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def emit(r: list[str]) -> None:
        cells = [_wrap_cell(c, widths[i]) for i, c in enumerate(r)]
        for k in range(max(len(c) for c in cells)):
            parts = []
            for i, c in enumerate(cells):
                t = c[k] if k < len(c) else ""
                parts.append(" " + t + " " * (widths[i] - text_width(t)) + " ")
            print("│" + "│".join(parts) + "│")

    if title:
        print(title)
    print(border("┌", "┬", "┐"))
    if headers:
        emit(headers)
        print(border("├", "┼", "┤"))
    for n, r in enumerate(rows):
        if n and not compact:
            print(border("├", "┼", "┤"))
        emit(r)
    print(border("└", "┴", "┘"))


_ROWS: list[list[str]] | None = None  # Rows collected by the active table.


def item_headers() -> list[str]:
    return ["Item", "Value"]


@contextlib.contextmanager
def table_rows(title: str = ""):
    """Collect rows before rendering a table."""
    global _ROWS
    outer, _ROWS = _ROWS, []
    try:
        yield _ROWS
        rows = _ROWS
    finally:
        _ROWS = outer
    print_table(item_headers(), rows, title)


def row(label: str, value: str = "") -> None:
    """Add a labeled value to the active table or render one row."""
    r = [label, str(value)]
    if _ROWS is not None:
        _ROWS.append(r)
    else:
        print_table(None, [r])


def row_more(lines: list[str]) -> None:
    """Append detail lines to the preceding row."""
    if _ROWS and lines:
        _ROWS[-1][1] += "\n" + "\n".join(lines)


def print_summary(cfg: Config, st: Stats, show: int = 20):
    def lst(items):
        more = [str(x) for x in items[:show]]
        if len(items) > show:
            more.append(f"… and {len(items) - show} more")
        row_more(more)

    def warn(text):
        row("⚠️ Note", text)

    print()
    with table_rows():
        _summary_rows(cfg, st, lst, warn)


def _summary_rows(cfg: Config, st: Stats, lst, warn):
    if st.error:
        row("Result", "❌ " + "Sync failed")
    elif st.ok:
        row("Result", "✅ " + "Sync complete"
            + (" (dry run, server not changed)" if st.dry else ""))
    else:
        row("Result", "⚠️ " + "Sync finished with problems, see below")
    if cfg.name:
        row("Task", cfg.name)
    row("Local folder", str(cfg.local_dir))
    row("Server", f"{cfg.username}@{cfg.host}:{cfg.remote_dir}")
    if filter_desc(cfg):
        row("Filter", filter_desc(cfg))
    for host, fp in NEW_HOST_KEYS:
        row("First connection", f"saved host key {fp}")
        row_more(["(compare it with the one from the server admin; a changed key will be refused later)"])
    if st.error:
        row("Reason", st.error)
    else:
        row("Files", f"local {st.local_files}, server {st.remote_files}")
        word = "To upload" if st.dry else "Uploaded"
        if st.uploaded:
            kinds: dict[str, int] = {}
            for why in st.uploaded.values():
                kinds[why] = kinds.get(why, 0) + 1
            detail = ", ".join(f"{k} {v}" for k, v in kinds.items())
            row(word, f"{len(st.uploaded)} ({detail})")
            lst([f"{r}  ({w})" for r, w in sorted(st.uploaded.items())])
            if st.upload_bytes:
                speed = fmt_bytes(st.upload_bytes / max(st.upload_seconds, 0.001))
                row("Transfer", f"{fmt_bytes(st.upload_bytes)}, average {speed}/s, {st.upload_seconds:.1f} s")
        else:
            row(word, "0, server is up to date")
        if st.deleted:
            row("To delete" if st.dry else "Deleted",
                f"{len(st.deleted)} (no longer exist locally)")
            lst(st.deleted)
        if st.delete_blocked:
            warn((f"Nothing deleted: this round would delete {st.delete_blocked} server files that no longer exist "
                              "locally, above the safety limit (the local folder is empty, or more than half of the server's "
                              "files). Check the local folder first (is the external drive connected?). To delete them, choose 1 "
                              "in the menu to sync this task now; it asks you to confirm"))
        if st.busy:
            row("Skipped", f"{st.busy} just-modified files (may still be written), next time")
        v = st.verify
        if v:
            if v.bad_final:
                row(v.kind, "❌ " + f"{len(v.bad_final)} files differ from local")
                lst(v.bad_final)
            if v.changed:
                warn(f"Changed locally during the sync: {len(v.changed)} (the server has the earlier version; sync again)")
                lst(v.changed)
            if v.unverified:
                warn((f"Not verified: {len(v.unverified)} (see the log); "
                                  "they are checked again next round"))
                lst(v.unverified)
            if v.bad_final or v.changed or v.unverified:
                pass
            elif v.repaired:
                row(v.kind, "✅ " + f"match ({len(v.repaired)} mismatched files were re-uploaded)")
                lst(v.repaired)
            else:
                row(v.kind, "✅ " + f"{v.files} files identical")
            if v.local_tree and not v.unverified:
                ok = v.local_tree == v.remote_tree
                comparison = "match" if ok else "MISMATCH"
                if v.repaired and ok:
                    comparison += " (after repair)"
                elif v.changed and not v.bad_final:
                    comparison += " (because the files above changed during the sync)"
                lt, rt = v.local_tree[:16], v.remote_tree[:16]
                row("Tree hash", f"local {lt}  server {rt}  {comparison}")
            if v.full:
                coverage = f"full mode, {v.hashed} hashed on the server"
            else:
                coverage = f"{v.hashed} hashed; {v.reused} unchanged on the server since the last check, previous result reused"
            if v.next_full:
                when = time.strftime("%m-%d %H:%M", time.localtime(max(v.next_full, time.time())))
                coverage += f" (next full check: {when})"
            row("Checked", coverage)
            row("Hash method", v.method)
        else:
            row("Verify", "nothing uploaded, not verified")
        for label, items in (("Upload failed", st.upload_failed),
                             ("Delete failed", st.delete_failed),
                             ("Conflict (a folder with this name on the server)", st.conflicts)):
            if items:
                row(label, f"❌ {len(items)}")
                lst(items)
    row("Time", f"{st.seconds:.1f} s")
    if cfg.log_file:
        row("Log file", cfg.log_file)


# ---------------------------------------------------------------- Sync round

MASS_DELETE_MIN = 20  # Require confirmation for a large majority of remote files.

def sync_once(cfg: Config, dry: bool = False, st: Stats | None = None) -> bool:
    st = st if st is not None else Stats()
    st.dry = dry
    t0 = time.time()
    local, local_paths, local_dirs = scan_local(cfg)
    conn = connect(cfg)
    fs = conn.fs
    try:
        # Scan only matching directories when remote deletion is disabled.
        remote, remote_dirs, paths = scan_remote(fs, cfg, None if cfg.delete_remote_extra else local_dirs)
        hasher = RemoteHasher(cfg, conn, paths)
        now = time.time()
        plan: list[tuple[str, str]] = []
        busy: set[str] = set()
        same_size: list[str] = []
        for rel, ent in local.items():
            # Defer files that may still be written.
            if 0 <= now - ent.mtime < cfg.settle_seconds:  # Future timestamps do not indicate an active write.
                busy.add(rel)
                continue
            if not fs.mtime_exact:
                # For coarse FTP timestamps, compare size and the cached local digest.
                rem = remote.get(rel)
                if rem is None:
                    plan.append((rel, "new"))
                elif rem.size != ent.size:
                    plan.append((rel, "size differs"))
                else:
                    same_size.append(rel)
                continue
            reason = needs_upload(cfg, ent, remote.get(rel))
            if reason:
                plan.append((rel, reason))
        if same_size:
            known = RemoteHashCache(cfg, hasher.algo).files
            lh = LocalHashCache(cfg, hasher.algo).hash_many({r: local_paths[r] for r in same_size if r in known})
            plan += [(r, "changed locally") for r in same_size if r in lh and lh[r] != known[r][1]]

        # Report remote directories that conflict with local files.
        conflicts = set(local) & remote_dirs
        for c in sorted(conflicts):
            log.error(f"Conflict: {c} is a folder on the server but a file locally, skipped")
        plan = [(r, why) for r, why in plan if r not in conflicts]

        extra = sorted(set(remote) - set(local)) if cfg.delete_remote_extra else []
        extra = [r for r in extra if not is_excluded(r, cfg.excludes) and file_selected(nfc(r), cfg)]
        # Protect against an empty source or a suspiciously large deletion set.
        if extra and not cfg.allow_mass_delete and (
                not local or (len(extra) > MASS_DELETE_MIN and len(extra) * 2 > len(remote))):
            st.delete_blocked = len(extra)
            log.warning((f"This round would delete {len(extra)} of {len(remote)} files on the server"
                          + (" (the local folder is empty)" if not local else "") + "; above the safety limit, nothing deleted"))
            extra = []

        st.local_files, st.remote_files, st.busy = len(local), len(remote), len(busy)
        st.conflicts = sorted(conflicts)
        st.deleted = list(extra) if dry else []
        log.info((f"Scan done: local {len(local)} files, server {len(remote)}; to upload {len(plan)}, to delete {len(extra)}"
                   + (f", {len(busy)} being written (next round)" if busy else "")))

        need_dirs = {posixpath.dirname(r) for r, _ in plan if "/" in r} | (local_dirs - remote_dirs)
        ensure_dirs(fs, cfg, need_dirs, remote_dirs, dry, paths)

        ok = True
        uploaded: set[str] = set()
        failed: set[str] = set()
        up_word = "[dry run] upload" if dry else "upload"
        for rel, why in plan:
            log.info("%s %s (%s)", up_word, rel, why)
        if plan and not dry:
            uploaded, ok = upload_batch(cfg, [r for r, _ in plan], local_paths, st, hasher)
            failed = {r for r, _ in plan} - uploaded
        st.uploaded = {r: why for r, why in plan if dry or r in uploaded}
        st.upload_failed = sorted(failed)

        targets = sorted(set(local) - busy - failed - conflicts)
        ok = verify_and_repair(cfg, conn, hasher, targets, local_paths, repair=not dry, st=st,
                               scanned=remote, fresh=uploaded, sizes={r: e.size for r, e in local.items()}) and ok
        if extra and not dry:
            if not ok or conflicts:
                log.warning("Upload or verification issues found; remote deletion skipped")
                extra = []
            else:
                current, _, _ = scan_local(cfg)
                if set(current) != set(local):
                    raise ScanError("The local file list changed during sync; remote deletion skipped")

        del_word = "[dry run] delete" if dry else "delete"
        for rel in extra:
            log.info("%s %s (%s)", del_word, rel, "gone locally")
            if not dry:
                try:
                    fs.remove(posixpath.join(cfg.remote_dir, paths[rel]))
                    st.deleted.append(rel)
                except OSError as e:
                    ok = False
                    st.delete_failed.append(rel)
                    log.error("%s %s: %s", "delete failed", rel, e)
        if extra and not dry:
            # Clean up only ancestors of deleted files that are absent locally.
            remove_empty_remote_dirs(fs, cfg, (ancestors(st.deleted) & remote_dirs) - local_dirs, paths)

        st.seconds = time.time() - t0
        log.info(f"Round took {st.seconds:.1f} s")
        return ok
    finally:
        conn.close()


def remove_empty_remote_dirs(fs: RemoteFS, cfg: Config, dirs: set[str], paths: dict[str, str]):
    for rel in sorted(dirs, key=lambda x: -x.count("/")):
        try:
            fs.rmdir(posixpath.join(cfg.remote_dir, paths[rel]))
            log.info("%s %s", "rmdir", rel)
        except OSError:
            pass  # Keep non-empty directories.


# ---------------------------------------------------------------- Main command

class ConsoleHandler(logging.StreamHandler):
    """Clear the active progress line before printing log output."""

    def emit(self, record):
        if PROGRESS_ACTIVE.is_set():
            try:
                self.stream.write("\r\033[K")
            except Exception:
                pass
        super().emit(record)


def setup_logging(cfg: Config, verbose: bool):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    h = ConsoleHandler(sys.stdout)
    h.setFormatter(fmt)
    root.addHandler(h)
    if cfg.log_file:
        os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=5_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def acquire_lock(cfg: Config, wait: bool = False):
    """Serialize sync rounds and return None if another process holds the lock."""
    f = open(state_dir() / f"{state_key(cfg)}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        return None
    return f


def explain_error(e: BaseException, cfg: Config) -> str:
    """Format a connection or sync failure for the operator."""
    if isinstance(e, ScanError):
        return str(e)
    if isinstance(e, paramiko.AuthenticationException):
        e = str(e).rstrip("\u3002.")
        return (f"Login failed. Check the username and password (or key): {e}. "
                  "If ssh cannot log in either, the server may have password login disabled (sshd PasswordAuthentication)")
    if isinstance(e, paramiko.BadHostKeyException):
        return ("The server's host key differs from the saved one; connection refused (protects against impostors)."
                + f"\n  {'Saved key'}: {fingerprint(e.expected_key)}"
                + f"\n  {'Current key'}: {fingerprint(e.key)}\n  "
                + ("If the server was reinstalled or its key changed: choose 7 in the menu to edit this task; "
                    "confirm when the connection test asks, and the new key is saved"))
    if isinstance(e, HostKeyUnknown):
        return (f"The host key {e.fp} of {e.hostname} is not known yet, and saving new host keys automatically is turned off.\n"
                  f"  Log in once with ssh -p {cfg.port} {cfg.username}@{cfg.host} and answer yes, "
                  f"or turn on \"save host key automatically\" in the task settings")
    if isinstance(e, SftpUnavailable):
        login = f"sftp -P {cfg.port} {cfg.username}@{cfg.host}"
        return (f"Logged in to {cfg.host}, but SFTP is not available ({e.detail}). Being able to log in with ssh does not "
                  "guarantee SFTP. Common causes:\n"
                  "  1. SFTP is not enabled on the server: /etc/ssh/sshd_config has no Subsystem sftp line, or sftp-server "
                  "is not installed (Ubuntu / Debian: sudo apt install openssh-sftp-server). Restart SSH afterwards: "
                  "sudo systemctl restart ssh\n"
                  "  2. A login file of the account such as .bashrc prints text at login, which corrupts the SFTP data: "
                  "make those files print nothing for non-interactive logins\n"
                  "  3. The account has a restricted shell, or the server only allows one command (ForceCommand in sshd_config)\n"
                  f"  Try it yourself in a terminal with {login}; the server administrator has to fix these.\n"
                  "  If the server has no SFTP at all (e.g. a router), change the task's connection method to \"SSH commands only\"")
    if isinstance(e, ShellUnavailable):
        return ("Logged in, but the server cannot run the stat command, so \"SSH commands only\" does not work. "
                  "If the server supports SFTP or FTP, choose that connection method in the task settings")
    if isinstance(e, paramiko.SSHException):
        if "dropped" in str(e).lower() or "closed" in str(e).lower():
            return f"The connection to the server was lost: {e}"
        return f"SSH error: {e}"
    if isinstance(e, CertUnknown):
        return (f"The FTPS certificate fingerprint {e.fp} of {e.hostname} is not known yet, and saving it automatically "
                  "is turned off. Turn on \"save host key automatically\" in the task's advanced options")
    if isinstance(e, CertChanged):
        return (f"The FTPS certificate of {e.hostname} differs from the saved one; connection refused (protects against impostors).\n"
                  f"  Saved: {e.expected}\n  Current: {e.got}\n"
                  "  If the certificate really changed (device reset, renewed certificate): choose 7 in the menu to edit this task; "
                  "confirm when the connection test asks, and the new certificate is saved")
    if isinstance(e, ftplib.Error):
        code = str(e)[:3]
        e = str(e).rstrip(".\u3002")
        if code == "530":
            return f"FTP login failed; check the username and password: {e}"
        if code == "421":
            return (f"The FTP server refused the connection (often too many simultaneous connections): {e}. "
                      "Lower \"parallel upload connections\" in the task's advanced options")
        if code in ("534", "431", "500", "502") and cfg.protocol == "ftps":
            return f"This FTP server does not support FTPS: {e}. On your own LAN you can switch the connection method to FTP"
        return f"FTP error: {e}"
    if isinstance(e, ssl.SSLError):
        return f"FTPS encrypted connection failed: {e}. The server may not support FTPS, or the port is wrong (FTPS usually uses port 21)"
    if isinstance(e, ConnectFailed):
        return f"Cannot connect to {cfg.host}:{cfg.port}; check the network, address and port: {e}"
    if isinstance(e, PermissionError):
        where = f"{e.filename}" if e.filename else str(e)
        return f"No permission on the server: {where}. Make sure the login account can read and write the server folder"
    if isinstance(e, TimeoutError):
        return (f"The server did not respond in time (longer than the connection timeout): {e}. "
                 "On slow networks or devices, raise the connection timeout in the task's advanced options")
    if isinstance(e, RemoteError):
        return f"An operation on the server failed: {e}"
    if isinstance(e, (ConnectionError, EOFError)):
        return f"The connection to the server was lost: {e}"
    if isinstance(e, OSError):
        return f"An operation on the server failed: {e}"
    return f"{'Error'}: {e}"


def run_round(cfg: Config, dry: bool = False) -> tuple[bool, Stats]:
    """Run one sync round and record its outcome."""
    log.info("%s %s → %s@%s:%s", "start sync", cfg.local_dir, cfg.username, cfg.host, cfg.remote_dir)
    if filter_desc(cfg):
        log.info("%s: %s", "filter", filter_desc(cfg))
    ok = False
    st = Stats()
    t0 = time.time()
    STOP.clear()
    try:
        ok = sync_once(cfg, dry=dry, st=st)
    except KeyboardInterrupt:
        st.error = "Interrupted (Ctrl+C or scheduled sync stopped)"
        log.warning(st.error)
    except Exception as e:
        st.error = explain_error(e, cfg)
        if isinstance(e, (paramiko.SSHException, OSError, EOFError, ftplib.Error, CertUnknown, CertChanged)):
            log.error(st.error)
        else:
            log.exception("%s: %s", "sync error", e)
    st.seconds = st.seconds or (time.time() - t0)
    return ok and st.ok, st


def save_summary(cfg: Config, st: Stats, show: bool) -> None:
    """Save the last result and print it for foreground runs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_summary(cfg, st)
    text = buf.getvalue()
    if show:
        sys.stdout.write(text)
        sys.stdout.flush()
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        (state_dir() / f"{state_key(cfg)}.last.txt").write_text(
            f"Synced at: {stamp}\n{text}", encoding="utf-8")
        save_json(state_dir() / f"{state_key(cfg)}.last.json",
                  {"time": time.time(), "status": st.status, "delete_blocked": st.delete_blocked})
    except OSError:
        pass


# ---------------------------------------------------------------- Scheduling

def daemon_file(cfg: Config) -> Path:
    return state_dir() / f"{state_key(cfg)}.daemon.json"


def _pid_is_ours(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return True
    return "sftp_sync.py" in cmd and "--loop" in cmd


def read_daemon(cfg: Config) -> dict | None:
    """Read state for a running scheduled task."""
    try:
        d = json.loads(daemon_file(cfg).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not _pid_is_ours(int(d.get("pid", 0))):
        daemon_file(cfg).unlink(missing_ok=True)
        return None
    return d


def fmt_time(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def start_daemon(cfg: Config, config_path: Path, minutes: int) -> int:
    d = read_daemon(cfg)
    if d:
        json_set(ok=True, state="already_running", minutes=d["minutes"], pid=d.get("pid"))
        daemon_table(cfg, "✅ " + "already running",
                     f"every {d['minutes']} min (since {fmt_time(d['started'])})",
                     "To change the interval, edit the task in the menu; it restarts automatically")
        return 0
    err = state_dir() / f"{state_key(cfg)}.daemon.err"
    cmd = [sys.executable, str(Path(__file__).resolve()), "-c", str(config_path), "--loop", str(minutes)]
    with open(err, "wb") as ef:
        # Detach the scheduled sync from the terminal.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef,
                                start_new_session=True, close_fds=True)
    for _ in range(50):
        time.sleep(0.2)
        if proc.poll() is not None:
            msg = err.read_text(errors="replace").strip()
            json_set(ok=False, state="failed", error=msg.splitlines()[-1] if msg else None)
            daemon_table(cfg, "❌ " + "failed to start", "",
                         msg.splitlines()[-1] if msg else "")
            return 1
        if read_daemon(cfg):
            break
    d = read_daemon(cfg) or {}
    json_set(ok=True, state="started", minutes=minutes, pid=d.get("pid"))
    daemon_table(cfg, "✅ " + "started; the first round starts now",
                 f"every {minutes} min",
                 ("Keeps running after this window is closed, until a reboot or \"Stop scheduled sync\". "
                   "Progress and results are shown in the menu"))
    return 0


JSON_RESULT: dict | None = None


def json_set(**kw) -> None:
    if JSON_RESULT is not None:
        JSON_RESULT.update(kw)


def iso_time(t: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t)) if t else None


def daemon_json(cfg: Config) -> dict:
    """Return the scheduling fields for JSON output."""
    d = read_daemon(cfg)
    if not d:
        return {"running": False}
    out = {"running": True, "pid": d.get("pid"), "minutes": d.get("minutes"), "started": iso_time(d.get("started")),
           "syncing": bool(d.get("running")), "next_run": iso_time(d.get("next_run")), "progress": None}
    try:
        pg = json.loads((state_dir() / f"{state_key(cfg)}.progress.json").read_text(encoding="utf-8"))
        if d.get("running") and time.time() - pg["time"] < 10:
            out["progress"] = pg["line"]
    except (OSError, ValueError, KeyError):
        pass
    return out


def stats_json(cfg: Config, st: Stats) -> dict:
    """Serialize the result of one sync round."""
    v = st.verify
    return {
        "ok": st.ok and not st.error, "status": st.status, "dry_run": st.dry, "error": st.error or None,
        "local_dir": str(cfg.local_dir),
        "server": {"protocol": cfg.protocol, "host": cfg.host, "port": cfg.port, "username": cfg.username,
                   "remote_dir": cfg.remote_dir},
        "filter": filter_desc(cfg) or None,
        "files": {"local": st.local_files, "remote": st.remote_files},
        "uploaded": [{"path": r, "reason": w} for r, w in sorted(st.uploaded.items())],
        "upload_bytes": st.upload_bytes, "upload_seconds": round(st.upload_seconds, 3),
        "deleted": list(st.deleted), "delete_blocked": st.delete_blocked,
        "upload_failed": list(st.upload_failed), "delete_failed": list(st.delete_failed),
        "conflicts": list(st.conflicts), "skipped_busy": st.busy,
        "verify": None if not v else {
            "kind": v.kind, "method": v.method, "files": v.files, "full": v.full,
            "hashed": v.hashed, "reused": v.reused, "next_full": iso_time(v.next_full),
            "local_tree": v.local_tree or None, "remote_tree": v.remote_tree or None,
            "tree_match": (v.local_tree == v.remote_tree) if v.local_tree and not v.unverified else None,
            "mismatched_then_repaired": list(v.repaired), "mismatched": list(v.bad_final),
            "changed_during_sync": list(v.changed), "unverified": list(v.unverified)},
        "new_host_keys": [{"host": h, "fingerprint": fp} for h, fp in NEW_HOST_KEYS],
        "seconds": round(st.seconds, 3), "log_file": cfg.log_file,
    }


def daemon_table(cfg: Config, state: str, interval: str = "", note: str = "") -> None:
    """Show the result of starting or stopping a scheduled task."""
    with table_rows():
        if cfg.name:
            row("Task", cfg.name)
        row("Scheduled sync", state)
        if interval:
            row("Interval", interval)
        if note:
            row("Note", note)


def stop_daemon(cfg: Config) -> int:
    d = read_daemon(cfg)
    if not d:
        json_set(ok=True, state="not_running")
        daemon_table(cfg, "not running")
        return 0
    pid = int(d["pid"])
    os.kill(pid, signal.SIGTERM)
    print("Stopping scheduled sync…")
    for i in range(300):  # Wait for the active transfers to stop.
        time.sleep(0.2)
        if not _pid_is_ours(pid):
            json_set(ok=True, state="stopped")
            daemon_table(cfg, "✅ " + "stopped")
            return 0
        if i == 50:
            print("(syncing, waiting for the current transfers to stop)")
    os.kill(pid, signal.SIGKILL)
    daemon_file(cfg).unlink(missing_ok=True)
    json_set(ok=True, state="force_stopped")
    daemon_table(cfg, "✅ " + "force-stopped", "",
                 "Staged files may remain on the server and will be verified before publication on the next upload")
    return 0


def _last_raw(cfg: Config) -> dict | None:
    try:
        return json.loads((state_dir() / f"{state_key(cfg)}.last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def last_json(cfg: Config) -> dict | None:
    """Return the previous sync result when available."""
    lj = _last_raw(cfg)
    if lj is None:
        return None
    return {"time": iso_time(lj.get("time")), "status": lj.get("status"), "delete_blocked": lj.get("delete_blocked", 0)}


def list_cmd() -> int:
    """List tasks and their schedules."""
    items, rows = [], []
    for i, path in enumerate(list_tasks(), 1):
        item = {"name": path.stem, "config": str(path.resolve())}
        try:
            cfg = load_config(path)
        except (SystemExit, Exception) as e:  # noqa: BLE001
            item.update(ok=False, error=str(e))
            items.append(item)
            rows.append([str(i), path.stem, "❗ " + str(e), "", ""])
            continue
        sch, last = daemon_json(cfg), last_json(cfg)
        item.update(ok=True, protocol=cfg.protocol, host=cfg.host, port=cfg.port, username=cfg.username,
                    local_dir=str(cfg.local_dir), remote_dir=cfg.remote_dir, filter=filter_desc(cfg) or None,
                    delete_remote_extra=cfg.delete_remote_extra, schedule=sch, last=last)
        items.append(item)
        if sch["running"]:
            state = "✅ " + f"every {sch['minutes']} min"
            state += ", syncing" if sch["syncing"] else ""
        else:
            state = "off"
        raw_last = _last_raw(cfg)
        icon = {"ok": "✅", "fail": "❌"}.get((raw_last or {}).get("status"), "⚠️")
        last_txt = f"{fmt_time(raw_last.get('time'))} {icon}" if raw_last else "never"
        rows.append([str(i), path.stem, state, last_txt,
                     f"{cfg.local_dir}\n→ {cfg.username}@{cfg.host}:{cfg.remote_dir}"])
    json_set(ok=True, tasks=items)
    if rows:
        print_table(["No.", "Task",
                     "Schedule", "Last sync",
                     "Local → server"], rows,
                    f"{len(rows)} task(s)")
    else:
        print("No sync task yet")
    return 0


def show_status(cfg: Config) -> int:
    json_set(ok=True, schedule=daemon_json(cfg), last=last_json(cfg))
    d = read_daemon(cfg)
    last = state_dir() / f"{state_key(cfg)}.last.txt"
    text = last.read_text(encoding="utf-8").rstrip() if last.exists() else ""
    with table_rows():
        if d:
            nxt, m, since = fmt_time(d.get("next_run")), d["minutes"], fmt_time(d["started"])
            status = "syncing" if d.get("running") else f"waiting, next at about {nxt}"
            row("Scheduled sync", f"✅ running (every {m} min, since {since}, {status})")
            try:
                pg = json.loads((state_dir() / f"{state_key(cfg)}.progress.json").read_text(encoding="utf-8"))
                if d.get("running") and time.time() - pg["time"] < 10:
                    row("Progress", pg["line"])
            except (OSError, ValueError, KeyError):
                pass
        else:
            row("Scheduled sync", "not running")
        if text:
            first, _, rest = text.partition("\n")
            stamp = first.split(":", 1)[1].strip() if ":" in first else first
            row("Last synced at", stamp)
            text = rest.strip("\n")
        else:
            row("Last sync", "not synced yet")
    if text:
        print("\n" + "Last sync result:")
        if "┌" in text:
            print(text)
        else:
            _old_summary_table(text)
    return 0


def _old_summary_table(text: str) -> None:
    """Display a saved result from an older release."""
    rows: list[list[str]] = []
    for ln in text.splitlines():
        t = ln.strip()
        if not t or set(t) == {"="}:
            continue
        m = re.match(r"^(\S.*?)(?:\uff1a|: )\s+(.*)$", t) if ln.startswith("  ") and not ln.startswith("   ") else None
        if m:
            rows.append([m.group(1), m.group(2)])
        elif not rows and t[:1] in "✅❌⚠":
            rows.append(["Result", t])
        elif t[:1] in "✅❌⚠":
            rows.append(["⚠️ Note", t])
        elif rows:
            rows[-1][1] += "\n" + t
        else:
            rows.append(["", t])
    print_table(item_headers(), rows)


def run_loop(config_path: Path, minutes: int) -> int:
    """Reload the task configuration and run each scheduled sync round."""
    stopping = False

    def on_stop(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            STOP.set()
            raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    cfg = load_config(config_path)
    setup_logging(cfg, False)
    info = {"pid": os.getpid(), "minutes": minutes, "started": time.time(),
            "config": str(config_path), "running": False, "next_run": None}
    dfile = daemon_file(cfg)

    def write_info():
        save_json(dfile, info)

    write_info()
    log.info(f"Scheduled sync started: every {minutes} min")
    try:
        while not stopping:
            try:
                cfg = load_config(config_path)
            except Exception as e:
                log.error("%s: %s", "bad settings, round skipped", e)
            except SystemExit as e:
                log.error("%s: %s", "bad settings, round skipped", e)
            else:
                lock = acquire_lock(cfg)
                if lock is None:
                    log.info("Another sync is running (maybe a manual one), round skipped")
                else:
                    try:
                        info["running"] = True
                        write_info()
                        _, st = run_round(cfg)
                        save_summary(cfg, st, show=False)
                    finally:
                        lock.close()
            next_run = time.time() + minutes * 60
            info.update(running=False, next_run=next_run, last_run=time.time())
            write_info()
            while not stopping and time.time() < next_run:
                time.sleep(min(5, max(0.1, next_run - time.time())))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if json.loads(dfile.read_text()).get("pid") == os.getpid():
                dfile.unlink()
        except (OSError, ValueError):
            pass
        log.info("Scheduled sync stopped")
    return 0


def schedule_minutes(config_path: Path, override: int | None) -> int:
    if override:
        return max(1, override)
    with open(config_path, "rb") as f:
        sch = tomllib.load(f).get("schedule", {})
    try:
        return max(1, int(sch.get("interval_minutes", 10)))
    except (TypeError, ValueError):
        raise SystemExit("[schedule] interval_minutes must be a number, e.g. interval_minutes = 10")


def list_tasks() -> list[Path]:
    return sorted(TASKS_DIR.glob("*.toml"), key=lambda p: p.stem) if TASKS_DIR.is_dir() else []


def resolve_config(config: str | None, task: str | None) -> Path:
    if task:
        p = TASKS_DIR / f"{task}.toml"
        if not p.exists():
            names = ", ".join(t.stem for t in list_tasks()) or "(no tasks yet)"
            raise SystemExit(f"No task named \"{task}\". Tasks: {names}")
        return p.resolve()
    if config:
        return Path(config).resolve()
    tasks = list_tasks()
    if len(tasks) == 1:
        return tasks[0].resolve()
    if tasks:
        names = ", ".join(t.stem for t in tasks)
        raise SystemExit(f"There are several tasks; use the menu (bash menu.sh) or add --task NAME. Tasks: {names}")
    for n in ("config.toml", "config.example.toml"):  # Legacy task settings.
        legacy = Path(__file__).resolve().with_name(n)
        try:
            with open(legacy, "rb") as f:
                host = tomllib.load(f).get("server", {}).get("host", "")
        except (OSError, ValueError):
            continue
        if host and host != "example.com":
            return legacy
    raise SystemExit("No sync task yet; open the menu (bash menu.sh) and create one")


def main():
    ap = argparse.ArgumentParser(description="Cli Sync · One-way folder sync to a server (SFTP / SSH / FTPS / FTP)")
    ap.add_argument("-c", "--config", help="settings file (default: the only task)")
    ap.add_argument("--task", metavar="NAME", help="use tasks/NAME.toml")
    ap.add_argument("--menu", action="store_true", help="open the menu")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done, change nothing")
    ap.add_argument("--full-verify", action="store_true",
                    help="recompute local and server file hashes this round")
    ap.add_argument("--allow-mass-delete", action="store_true", help=argparse.SUPPRESS)  # Set only after confirmation in the menu.
    ap.add_argument("-v", "--verbose", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--start", nargs="?", type=int, const=0, metavar="MIN",
                   help="start scheduled sync (default: the task's interval)")
    g.add_argument("--stop", action="store_true", help="stop scheduled sync")
    g.add_argument("--status", action="store_true", help="show scheduled sync status and last result")
    g.add_argument("--list", action="store_true", help="list all tasks and their status")
    g.add_argument("--delete-task", action="store_true",
                   help="delete the --task task (needs --yes; files on the server stay)")
    g.add_argument("--rename", metavar="NEW", help="rename the --task task to NEW")
    g.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="create or edit a task without the menu (with --task, repeatable); creates it if missing")
    g.add_argument("--loop", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--yes", action="store_true", help="confirm deleting a task")
    ap.add_argument("--password-stdin", action="store_true",
                    help="with --set: read the login password from stdin")
    ap.add_argument("--test-connection", action="store_true",
                    help="with --set: connect to the server first and save only if that works")
    ap.add_argument("--print-log-path", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true",
                    help=("for programs/agents: stdout is a single JSON result, human output goes to stderr; "
                           "exit 0 = ok, 1 = failed or problems"))
    args = ap.parse_args()
    if args.json and not (args.menu or args.loop or args.print_log_path):
        sys.exit(main_json(args))
    _main(args)


def main_json(args) -> int:
    """Write one JSON result to stdout and send human output to stderr."""
    global JSON_RESULT
    out = sys.stdout
    cmd = ("delete_task" if args.delete_task else "rename" if args.rename else "list" if args.list else "set" if args.set or args.password_stdin else "start" if args.start is not None else "stop" if args.stop else "status" if args.status
           else "dry_run" if args.dry_run else "full_verify" if args.full_verify else "sync")
    JSON_RESULT = {"command": cmd, "task": None, "ok": False}
    code = 1
    sys.stdout = sys.stderr
    try:
        _main(args)
        code = 0
    except SystemExit as e:
        if isinstance(e.code, str):
            JSON_RESULT.update(ok=False, error=e.code, tasks=[t.stem for t in list_tasks()])
            code = 1
        else:
            code = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        JSON_RESULT.update(ok=False, error=f"{type(e).__name__}: {e}")
        code = 1
    finally:
        sys.stdout = out
    if code and JSON_RESULT.get("ok"):
        JSON_RESULT["ok"] = False
    out.write(json.dumps(JSON_RESULT, ensure_ascii=False, indent=2) + "\n")
    out.flush()
    return code


def _main(args):

    if args.menu:
        import tasks_ui
        sys.exit(tasks_ui.menu())
    if args.list:
        sys.exit(list_cmd())
    if args.delete_task or args.rename:
        if not args.task:
            raise SystemExit("Use --task to say which task")
        if args.dry_run or args.full_verify or args.password_stdin or args.test_connection:
            raise SystemExit("--delete-task / --rename cannot be combined with sync or --set options")
        import tasks_ui
        code, res = (tasks_ui.delete_task_cli(args.task, args.yes) if args.delete_task
                     else tasks_ui.rename_task_cli(args.task, args.rename))
        json_set(**res)
        sys.exit(code)
    if args.set or args.password_stdin or args.test_connection:
        if not args.set and not args.password_stdin:
            raise SystemExit("--test-connection needs --set")
        if not args.task:
            raise SystemExit("--set needs --task NAME (the task is created if missing)")
        if args.dry_run or args.full_verify:
            raise SystemExit("--set cannot be combined with --dry-run or --full-verify")
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None
        import tasks_ui
        code, res = tasks_ui.set_task_cli(args.task, args.set or [], password, args.test_connection)
        json_set(**res)
        sys.exit(code)
    config_path = resolve_config(args.config, args.task)

    if args.print_log_path:
        with open(config_path, "rb") as f:
            lg = tomllib.load(f).get("log", {})
        print(os.path.expanduser(lg.get("file") or default_log_file(task_name_of(config_path)))
              if lg.get("file", True) else "")
        return
    if args.loop:
        sys.exit(run_loop(config_path, args.loop))

    cfg = load_config(config_path)
    json_set(task=cfg.name or task_name_of(config_path), config=str(config_path))
    if args.start is not None:
        sys.exit(start_daemon(cfg, config_path, schedule_minutes(config_path, args.start)))
    if args.stop:
        sys.exit(stop_daemon(cfg))
    if args.status:
        sys.exit(show_status(cfg))

    cfg.force_full_verify = args.full_verify
    cfg.allow_mass_delete = args.allow_mass_delete
    setup_logging(cfg, args.verbose)
    _lock = acquire_lock(cfg)
    if _lock is None:
        json_set(busy=True)
        raise SystemExit("Another sync is running (maybe a scheduled round); try again when it finishes")
    ok, st = run_round(cfg, dry=args.dry_run)
    json_set(**stats_json(cfg, st))
    if JSON_RESULT is not None:
        JSON_RESULT["ok"] = ok
    if args.dry_run:
        print_summary(cfg, st)
    else:
        save_summary(cfg, st, show=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
