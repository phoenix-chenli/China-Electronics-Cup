"""Small OS adapters: real advisory locks and private coordination files.

Lock files are never unlinked: changing their inode would split contenders.
Windows uses a nonblocking byte-range lock; POSIX uses flock. Both are released
by the OS on process exit, including abnormal exit.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import subprocess
import time


def secure_open(path, flags, mode=0o600):
    path = Path(path)
    # Windows has no O_NOFOLLOW. Reject links/reparse points before and after
    # opening; these files live within an owner-only directory, not shared temp.
    def check():
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise OSError('协调文件不能是符号链接或重解析点')
        if not stat.S_ISREG(info.st_mode):
            raise OSError('协调文件必须是普通文件')
    check()
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), mode)
    try:
        check()
        if os.name != 'nt':
            os.fchmod(fd, mode)
        return fd
    except BaseException:
        os.close(fd)
        raise


def ensure_private_directory(path):
    path = Path(path)
    created = not path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise OSError('模型共享排队目录不能是符号链接或重解析点')
    if os.name != 'nt':
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError('模型共享排队目录须为当前用户私有目录（0700）')
        return path
    # Use the current SID (locale independent). New folders get protected ACLs;
    # existing folders are checked, never silently loosened or rewritten.
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:ZHIHENG_PRIVATE_DIRECTORY_CHECK
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
if ($env:ZHIHENG_PRIVATE_DIRECTORY_NEW -eq '1') {
  $acl = New-Object Security.AccessControl.DirectorySecurity
  $acl.SetOwner($sid)
  $acl.SetAccessRuleProtection($true, $false)
  $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
  $acl.AddAccessRule($rule)
  Set-Acl -LiteralPath $p -AclObject $acl
}
$acl = Get-Acl -LiteralPath $p
if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { exit 3 }
$allowed = @($sid.Value, 'S-1-5-18', 'S-1-5-32-544')
foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
  if ($rule.AccessControlType -eq 'Allow' -and $allowed -notcontains $rule.IdentityReference.Value) { exit 4 }
}
'''
    env = dict(os.environ, ZHIHENG_PRIVATE_DIRECTORY_CHECK=str(path),
               ZHIHENG_PRIVATE_DIRECTORY_NEW='1' if created else '0')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            env=env, capture_output=True, timeout=15)
    if result.returncode:
        raise OSError('Windows共享排队目录须由当前用户拥有，且不允许其他普通用户访问')
    return path


def try_lock_fd(fd):
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise


def unlock_fd(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class FileLock:
    def __init__(self, path, timeout=0):
        self.path, self.timeout = Path(path), timeout
        self.fd = None

    def acquire(self):
        if self.fd is not None:
            raise RuntimeError('锁已由当前对象持有')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = secure_open(self.path, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + self.timeout
        try:
            while not try_lock_fd(fd):
                if time.monotonic() >= deadline:
                    raise BlockingIOError('另一个进程正在使用此运行目录')
                time.sleep(.05)
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                unlock_fd(fd)
            finally:
                os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
