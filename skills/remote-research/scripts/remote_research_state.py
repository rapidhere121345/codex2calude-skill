"""Private broker state and pinned SSH configuration (standard library only)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

HOST = "44.255.229.140"
PORT = "22022"
KNOWN_HOSTS_LINE = (
    "[44.255.229.140]:22022 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOposESz9sift7F1deDCPoJZucWQ69k1Ln4b1auELh5s"
)
PRINCIPALS = ("owner", "partner", "partner2", "partner3", "partner4")
MAX_LINE = 32 * 1024 * 1024
PROTOCOL = 1


def windows_owned_by_current_user(path: Path) -> bool:
    """Compare the file owner SID to the process token user SID."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    if advapi.GetNamedSecurityInfoW(
        str(path), 1, 1, ctypes.byref(owner), None, None, None,
        ctypes.byref(descriptor),
    ):
        return False
    token = wintypes.HANDLE()
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = wintypes.BOOL
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            return False
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(
            token, 1, buffer, size, ctypes.byref(size)
        ):
            return False
        user_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return bool(advapi.EqualSid(owner, user_sid))
    finally:
        if token.value:
            kernel.CloseHandle(token)
        kernel.LocalFree(descriptor)


def secure_windows_acl(path: Path) -> None:
    # No inherited ACEs, and no access by other local users. A failed
    # ACL operation is fatal rather than silently falling back to chmod.
    user = os.environ.get("USERNAME")
    if not user or not windows_owned_by_current_user(path):
        raise ValueError("Cannot confirm Windows state owner")
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if result.returncode:
        raise ValueError("Cannot secure Windows state ACL")
    acl = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=False,
    )
    aces = re.findall(r"([^\s:]+):\([^)]+\)", acl.stdout)
    if acl.returncode or not aces or any(
        ace.split("\\")[-1].lower() != user.lower() for ace in aces
    ):
        raise ValueError("Unexpected Windows state ACL")


def private(path: Path, directory: bool = False) -> None:
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise ValueError("Symlinked parent directory")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    ):
        raise ValueError("Unexpected state file type")
    if os.name == "nt":
        secure_windows_acl(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("State must be owned by the current user and private")


def secure_dir(path: Path) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Symlinked state directory")
    path.mkdir(mode=0o700, exist_ok=True)
    private(path, directory=True)


def parent_dir(path: Path) -> None:
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Symlinked state parent")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Invalid state parent")
    if os.name != "nt" and path.stat().st_uid != os.getuid():
        raise ValueError("Foreign-owned state parent")


def state_root() -> Path:
    override = os.environ.get("REMOTE_RESEARCH_STATE_DIR")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            raise ValueError("State directory must be absolute")
        parent_dir(root.parent)
    elif os.name == "nt":
        root = Path(os.environ["LOCALAPPDATA"]) / "remote-research"
        parent_dir(root.parent)
    else:
        home = Path.home().resolve()
        # XDG defaults are not automatically trusted; secure only the
        # directories this application creates/uses.
        base = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
        if not base.is_absolute():
            raise ValueError("XDG_STATE_HOME must be absolute")
        if base == home / ".local" / "state":
            parent_dir(home / ".local")
        parent_dir(base)
        root = base / "remote-research"
    secure_dir(root)
    return root


def read_config(path: Path) -> dict:
    if not path.is_absolute():
        raise ValueError("Config path must be absolute")
    private(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Invalid client config version")
    if config.get("principal") not in PRINCIPALS:
        raise ValueError("Invalid principal")
    cmd = config.get("ssh_command")
    if not isinstance(cmd, list) or not all(isinstance(a, str) and a for a in cmd):
        raise ValueError("Invalid SSH argv")
    if Path(cmd[0]).name.lower() not in ("ssh", "ssh.exe"):
        raise ValueError("Only SSH is supported")
    required = ("-F", "none", "-T", "-p", PORT, "-i", "-o",
                "BatchMode=yes", "IdentitiesOnly=yes",
                "StrictHostKeyChecking=yes", "ClearAllForwardings=yes",
                "RequestTTY=no", f"researchmcp@{HOST}")
    if not all(item in cmd for item in required) or cmd[-1] != required[-1]:
        raise ValueError("SSH pin or dedicated account missing")
    if cmd.count("-i") != 1 or cmd.count("-p") != 1:
        raise ValueError("Conflicting SSH options")
    options = [cmd[i + 1] for i, value in enumerate(cmd[:-1]) if value == "-o"]
    if len(options) != 9 or any(
        not any(opt.startswith(prefix) for prefix in (
            "BatchMode=", "IdentitiesOnly=", "StrictHostKeyChecking=",
            "UserKnownHostsFile=", "ConnectTimeout=", "ServerAliveInterval=",
            "ServerAliveCountMax=", "ClearAllForwardings=", "RequestTTY=",
        )) for opt in options
    ):
        raise ValueError("Unexpected SSH options")
    # Exact allowlist: no remote command, -W, ControlMaster or alternate config.
    expected = ["-F", "none", "-T", "-p", PORT, "-i", cmd[cmd.index("-i") + 1]]
    expected += [item for option in options for item in ("-o", option)]
    expected += [f"researchmcp@{HOST}"]
    if cmd[1:] != expected or len(set(opt.split("=")[0] for opt in options)) != len(options):
        raise ValueError("Unexpected SSH argv")
    key = Path(cmd[cmd.index("-i") + 1])
    host = Path(next(o.split("=", 1)[1] for o in options if o.startswith("UserKnownHostsFile=")))
    if not key.is_absolute() or not host.is_absolute():
        raise ValueError("SSH paths must be absolute")
    private(key)
    private(host)
    if host.read_bytes() not in (
        KNOWN_HOSTS_LINE.encode(), (KNOWN_HOSTS_LINE + "\n").encode()
    ):
        raise ValueError("SSH host pin mismatch")
    return config


def state_paths(config: dict) -> tuple[Path, Path, Path]:
    digest = hashlib.sha256(json.dumps(
        [config["principal"], config["ssh_command"], KNOWN_HOSTS_LINE],
        separators=(",", ":"),
    ).encode()).hexdigest()
    root = state_root() / digest
    secure_dir(root)
    return root / "broker.json", root / "start.lock", root


@contextlib.contextmanager
def startup_lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        private(path)
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            if not os.read(fd, 1):
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_private(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ValueError("Symlinked output")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (
            os.name != "nt" and info.st_uid != os.getuid()
        ):
            raise ValueError("Invalid output owner or file type")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".remote-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(name, 0o600)
        else:
            private(Path(name))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
