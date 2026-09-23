"""One explicit writable root, no symlinks or hardlinked mutable files."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath
import stat
from uuid import uuid4

APP_ROOT = Path(os.environ.get("RESEARCH_INTEL_HOME", Path.cwd())).absolute()


class BoundaryError(ValueError):
    pass


def checked_root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.resolve() != root or root.is_symlink():
        raise BoundaryError("Workspace root symlinks are denied")
    if not root.is_dir():
        raise BoundaryError("Root must already exist")
    return root


class Workspace:
    def __init__(self, root: Path = APP_ROOT):
        self.root = checked_root(root)

    @staticmethod
    def parts(relative: str) -> tuple[str, ...]:
        p = PurePosixPath(relative)
        if not relative or p.is_absolute() or ".." in p.parts or "\\" in relative or "\x00" in relative:
            raise BoundaryError("Invalid workspace-relative path")
        if not p.parts or any(x in {"", "."} for x in relative.split("/")):
            raise BoundaryError("Invalid path component")
        return p.parts

    @contextmanager
    def parent_fd(self, relative: str, *, create: bool = False):
        parts = self.parts(relative)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in parts[:-1]:
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, parts[-1]
        except OSError as exc:
            raise BoundaryError("Unsafe or inaccessible workspace path") from exc
        finally:
            os.close(fd)

    def checked_path(self, relative: str, *, create_parent: bool = False) -> Path:
        with self.parent_fd(relative, create=create_parent) as (fd, name):
            try:
                s = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    raise BoundaryError("Mutable target must be a regular, non-hardlinked file")
        return self.root.joinpath(*self.parts(relative))

    def read(self, relative: str, *, limit: int = 8 * 1024 * 1024) -> bytes:
        with self.parent_fd(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                    raise BoundaryError("Invalid or oversized input")
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise BoundaryError("Input exceeds limit")
                return data

    def write(self, relative: str, data: bytes, *, replace: bool = False) -> Path:
        if not isinstance(data, bytes):
            raise TypeError("bytes required")
        with self.parent_fd(relative, create=True) as (parent, name):
            try:
                s = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                s = None
            if s is not None:
                if not replace or not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                    raise BoundaryError("Existing/unsafe destination denied")
            temporary = f".pending-{uuid4().hex}"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if replace:
                    os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return self.root.joinpath(*self.parts(relative))
