import ftplib
import os
import posixpath
import ssl
import stat
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sftp_sync as sync


class LocalRemote(sync.RemoteFS):
    """Run remote file operations in a separate temporary directory."""

    def __init__(self, root):
        self.root = root

    def path(self, name):
        return self.root / name.lstrip("/")

    def stat(self, path):
        attrs = self.path(path).lstat()
        return sync.RAttr(posixpath.basename(path), attrs.st_mode, attrs.st_size, int(attrs.st_mtime))

    def listdir_attr(self, path):
        return [self.stat(posixpath.join(path, p.name)) for p in self.path(path).iterdir()]

    def mkdir(self, path, mode=0o777):
        self.path(path).mkdir(mode=mode)

    def remove(self, path):
        self.path(path).unlink()

    def rmdir(self, path):
        self.path(path).rmdir()

    def rename(self, src, dst):
        self.path(src).rename(self.path(dst))

    def set_attrs(self, path, mtime, mode):
        os.utime(self.path(path), (mtime, mtime))

    def write_file(self, local_path, path, offset, cb=None, throttle=None):
        with self.path(path).open("ab" if offset else "wb") as stream:
            stream.write(local_path.read_bytes()[offset:])

    def read_into(self, path, cb):
        cb(self.path(path).read_bytes())


class SyncTests(unittest.TestCase):
    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def setUp(self):
        tmp = self.enter(tempfile.TemporaryDirectory())
        root = Path(tmp)
        local = root / "local"
        local.mkdir()
        self.fs = LocalRemote(root)
        self.fs.mkdir("/remote")
        self.cfg = sync.Config(local, "example.invalid", "/remote", delete_remote_extra=True,
                               settle_seconds=0, workers=1)
        self.conn = sync.Conn(self.fs)
        self.local = local / "file.txt"
        self.local.write_bytes(b"new data")
        self.enter(patch.object(sync, "state_dir", return_value=root))
        self.enter(patch.object(sync, "connect", return_value=self.conn))
        self.enter(patch.object(sync, "conn_alive", return_value=True))
        self.enter(patch.object(sync.Progress, "start"))
        self.enter(patch.object(sync.Progress, "stop"))
        self.enter(patch.object(sync.log, "disabled", True))

    def test_incomplete_local_scan_stops_before_connecting(self):
        def walk(root, *, followlinks, onerror):
            yield str(root), ["unreadable"], [self.local.name]
            onerror(PermissionError("unreadable directory"))

        with patch.object(sync.os, "walk", walk), patch.object(sync, "connect") as connect:
            ok, result = sync.run_round(self.cfg)
        self.assertFalse(ok)
        self.assertFalse(result.deleted)
        connect.assert_not_called()

    def test_legacy_temp_is_preserved_during_successful_sync(self):
        legacy = self.fs.path("/remote/notes.sftpsync-tmp")
        legacy.write_bytes(b"keep")
        extra = self.fs.path("/remote/extra.txt")
        extra.write_bytes(b"obsolete")
        ok, result = sync.run_round(self.cfg)
        self.assertTrue(ok, result.error)
        self.assertEqual(result.deleted, ["extra.txt"])
        self.assertFalse(extra.exists())
        self.assertEqual(b"keep", legacy.read_bytes())
        self.assertEqual(self.local.read_bytes(), self.fs.path("/remote/file.txt").read_bytes())

    def test_failed_publication_restores_old_destination(self):
        old, new, backup = "/remote/file.txt", "/remote/staged", "/remote/rollback"
        self.fs.path(old).write_bytes(b"old")
        self.fs.path(new).write_bytes(b"new")
        rename = self.fs.rename

        def reject_publication(src, dst):
            if src == new:
                raise OSError("rename rejected")
            rename(src, dst)

        with patch.object(self.fs, "rename", side_effect=reject_publication):
            with self.assertRaises(OSError):
                self.fs.replace(new, old, backup)
        self.assertEqual(self.fs.path(old).read_bytes(), b"old")
        self.assertEqual(self.fs.path(new).read_bytes(), b"new")
        self.assertFalse(self.fs.path(backup).exists())

    def test_interrupted_rollback_can_be_recovered(self):
        old, backup = "/remote/file.txt", "/remote/rollback"
        self.fs.path(backup).write_bytes(b"old")
        self.fs.recover_replace(old, backup)
        self.assertEqual(self.fs.path(old).read_bytes(), b"old")
        self.assertFalse(self.fs.path(backup).exists())

    def test_sftp_prefers_atomic_rename(self):
        client = Mock()
        client.remove.side_effect = FileNotFoundError
        sync.SftpFS(client).replace("staged", "destination", "rollback")
        client.posix_rename.assert_called_once_with("staged", "destination")
        client.rename.assert_not_called()
        client.remove.assert_called_once_with("rollback")

    def test_resumed_staging_is_checked_and_published(self):
        dst = "/remote/file.txt"
        staged, _ = sync.staging_paths(self.fs, dst, sync.state_key(self.cfg))
        self.fs.path(staged).write_bytes(self.local.read_bytes()[:3])
        with patch.object(self.fs, "write_file", wraps=self.fs.write_file) as write:
            ok, result = sync.run_round(self.cfg)
        self.assertTrue(ok, result.error)
        self.assertEqual(write.call_args.args[2], 3)
        self.assertEqual(self.fs.path(dst).read_bytes(), self.local.read_bytes())
        self.assertFalse(self.fs.path(staged).exists())

    def test_corrupt_staging_keeps_destination_and_skips_deletion(self):
        dst = self.fs.path("/remote/file.txt")
        dst.write_bytes(b"old")
        extra = self.fs.path("/remote/extra")
        extra.write_bytes(b"keep")
        write = self.fs.write_file

        def corrupt(local_path, path, offset, cb, throttle):
            write(local_path, path, offset, cb, throttle)
            self.fs.path(path).write_bytes(b"x" * local_path.stat().st_size)

        with patch.object(self.fs, "write_file", side_effect=corrupt), patch.object(sync.time, "sleep"):
            ok, result = sync.run_round(self.cfg)
        self.assertFalse(ok)
        self.assertEqual(result.upload_failed, [self.local.name])
        self.assertEqual(dst.read_bytes(), b"old")
        self.assertTrue(extra.exists())

    def test_equivalent_remote_name_reused_and_collision_rejected(self):
        name = "café.txt"
        nfd = unicodedata.normalize("NFD", name)
        self.local.rename(self.cfg.local_dir / name)
        attrs = sync.RAttr(nfd, stat.S_IFREG, len(b"new data"), int((self.cfg.local_dir / name).stat().st_mtime))
        with patch.object(self.fs, "listdir_attr", return_value=[attrs]), \
                patch.object(sync, "verify_and_repair", return_value=True):
            ok, result = sync.run_round(self.cfg, dry=True)
        self.assertTrue(ok)
        self.assertFalse(result.uploaded)
        self.assertFalse(result.deleted)
        with patch.object(self.fs, "listdir_attr", return_value=[attrs, sync.RAttr(name, stat.S_IFREG, 1, 0)]):
            ok, result = sync.run_round(self.cfg)
        self.assertFalse(ok)
        self.assertFalse(result.deleted)

    def test_failed_local_hash_is_reported(self):
        hasher = sync.RemoteHasher(self.cfg, self.conn)
        result = sync.Stats()
        with patch.object(sync, "hash_file", side_effect=PermissionError("unreadable")):
            self.assertFalse(sync.verify_and_repair(self.cfg, self.conn, hasher, [self.local.name],
                                                   {self.local.name: self.local}, True, result))
        self.assertEqual(result.verify.unverified, [self.local.name])
        self.assertIsNone(sync.stats_json(self.cfg, result)["verify"]["tree_match"])

    def test_failed_second_verification_is_reported(self):
        hasher = sync.RemoteHasher(self.cfg, self.conn)
        name = self.local.name
        result = sync.Stats()
        with patch.object(hasher, "hashes", side_effect=[({name: "mismatch"}, set()), ({}, {name})]), \
                patch.object(sync, "upload_batch", return_value=({name}, True)):
            self.assertFalse(sync.verify_and_repair(self.cfg, self.conn, hasher, [name],
                                                   {name: self.local}, True, result))
        self.assertEqual(result.verify.unverified, [name])
        self.assertEqual(result.verify.repaired, [])
        self.assertFalse(result.ok)

    def test_local_file_created_during_sync_cancels_deletion(self):
        extra = self.fs.path("/remote/extra")
        extra.write_bytes(b"keep")
        self.fs.path("/remote/file.txt").write_bytes(self.local.read_bytes())

        def verify(*args, **kwargs):
            (self.cfg.local_dir / "extra").write_bytes(b"keep")
            return True

        with patch.object(sync, "verify_and_repair", side_effect=verify):
            ok, result = sync.run_round(self.cfg)
        self.assertFalse(ok)
        self.assertFalse(result.deleted)
        self.assertTrue(extra.exists())

    def test_full_verification_bypasses_local_cache(self):
        name = self.local.name
        cache = sync.LocalHashCache(self.cfg)
        cache.data[name] = [sync.file_signature(self.local.stat()), "stale"]
        cache.save()
        self.cfg.force_full_verify = True
        self.fs.path("/remote/file.txt").write_bytes(self.local.read_bytes())
        with patch.object(sync, "hash_file", wraps=sync.hash_file) as hash_file:
            ok, result = sync.run_round(self.cfg)
        self.assertTrue(ok)
        self.assertTrue(result.verify.full)
        self.assertFalse(result.verify.repaired)
        hash_file.assert_called_once_with(self.local, "sha256")


class LanguageTests(unittest.TestCase):
    def test_cli_help_is_english_with_a_legacy_language_override(self):
        result = subprocess.run([sys.executable, "-B", sync.__file__, "--help"],
                                env={**os.environ, "SFTP_SYNC_LANG": "zh"},
                                text=True, capture_output=True, check=True)
        self.assertIn("One-way folder sync to a server", result.stdout)
        self.assertNotRegex(result.stdout, r"[\u3400-\u9fff]")


class FTPSTests(unittest.TestCase):
    def test_data_channel_identity(self):
        for reused, cert, accepted in ((True, None, True), (False, b"pinned", True),
                                       (False, None, False), (False, b"different", False)):
            with self.subTest(reused=reused, cert=cert):
                context = Mock()
                client = sync._FTP_TLS(context=context)
                client._prot_p = True
                client.sock = Mock()
                client.sock.getpeercert.return_value = b"pinned"
                data = context.wrap_socket.return_value
                data.session_reused = reused
                data.getpeercert.return_value = cert
                with patch.object(ftplib.FTP, "ntransfercmd", return_value=(Mock(), 12)):
                    if accepted:
                        self.assertEqual(client.ntransfercmd("RETR file"), (data, 12))
                    else:
                        with self.assertRaises(ssl.SSLError):
                            client.ntransfercmd("RETR file")
                        data.close.assert_called_once()
                        self.assertIsNone(client.sock)


if __name__ == "__main__":
    unittest.main()
