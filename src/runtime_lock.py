"""Nonblocking process lock shared by the pipeline and maintenance CLI."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def pipeline_lock(state_path):
    path = Path(str(state_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Pipeline or maintenance operation already running; stop it first") from None
        yield
    finally:
        os.close(fd)  # closing releases flock; never unlink an active lock inode
