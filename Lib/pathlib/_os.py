"""
Low-level OS functionality wrappers used by pathlib.
"""

from errno import *
import os
import stat
import sys
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import posix
except ImportError:
    posix = None
try:
    import _winapi
except ImportError:
    _winapi = None


def get_copy_blocksize(infd):
    """Determine blocksize for fastcopying on Linux.
    Hopefully the whole file will be copied in a single call.
    The copying itself should be performed in a loop 'till EOF is
    reached (0 return) so a blocksize smaller or bigger than the actual
    file size should not make any difference, also in case the file
    content changes while being copied.
    """
    try:
        blocksize = max(os.fstat(infd).st_size, 2 ** 23)  # min 8 MiB
    except OSError:
        blocksize = 2 ** 27  # 128 MiB
    # On 32-bit architectures truncate to 1 GiB to avoid OverflowError,
    # see gh-82500.
    if sys.maxsize < 2 ** 32:
        blocksize = min(blocksize, 2 ** 30)
    return blocksize


if fcntl and hasattr(fcntl, 'FICLONE'):
    def clonefd(source_fd, target_fd):
        """
        Perform a lightweight copy of two files, where the data blocks are
        copied only when modified. This is known as Copy on Write (CoW),
        instantaneous copy or reflink.
        """
        fcntl.ioctl(target_fd, fcntl.FICLONE, source_fd)
else:
    clonefd = None


if posix and hasattr(posix, '_fcopyfile'):
    def copyfd(source_fd, target_fd):
        """
        Copy a regular file content using high-performance fcopyfile(3)
        syscall (macOS).
        """
        posix._fcopyfile(source_fd, target_fd, posix._COPYFILE_DATA)
elif hasattr(os, 'copy_file_range'):
    def copyfd(source_fd, target_fd):
        """
        Copy data from one regular mmap-like fd to another by using a
        high-performance copy_file_range(2) syscall that gives filesystems
        an opportunity to implement the use of reflinks or server-side
        copy.
        This should work on Linux >= 4.5 only.
        """
        blocksize = get_copy_blocksize(source_fd)
        offset = 0
        while True:
            sent = os.copy_file_range(source_fd, target_fd, blocksize,
                                      offset_dst=offset)
            if sent == 0:
                break  # EOF
            offset += sent
elif hasattr(os, 'sendfile'):
    def copyfd(source_fd, target_fd):
        """Copy data from one regular mmap-like fd to another by using
        high-performance sendfile(2) syscall.
        This should work on Linux >= 2.6.33 only.
        """
        blocksize = get_copy_blocksize(source_fd)
        offset = 0
        while True:
            sent = os.sendfile(target_fd, source_fd, offset, blocksize)
            if sent == 0:
                break  # EOF
            offset += sent
else:
    copyfd = None


if _winapi and hasattr(_winapi, 'CopyFile2') and hasattr(os.stat_result, 'st_file_attributes'):
    def _is_dirlink(path):
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            return False
        return (st.st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY and
                st.st_reparse_tag == stat.IO_REPARSE_TAG_SYMLINK)

    def copyfile(source, target, follow_symlinks):
        """
        Copy from one file to another using CopyFile2 (Windows only).
        """
        if follow_symlinks:
            flags = 0
        else:
            flags = _winapi.COPY_FILE_COPY_SYMLINK
            try:
                _winapi.CopyFile2(source, target, flags)
                return
            except OSError as err:
                # Check for ERROR_ACCESS_DENIED
                if err.winerror != 5 or not _is_dirlink(source):
                    raise
            flags |= _winapi.COPY_FILE_DIRECTORY
        _winapi.CopyFile2(source, target, flags)
else:
    copyfile = None


def copyfileobj(source_f, target_f):
    """
    Copy data from file-like object source_f to file-like object target_f.
    """
    try:
        source_fd = source_f.fileno()
        target_fd = target_f.fileno()
    except Exception:
        pass  # Fall through to generic code.
    else:
        try:
            # Use OS copy-on-write where available.
            if clonefd:
                try:
                    clonefd(source_fd, target_fd)
                    return
                except OSError as err:
                    if err.errno not in (EBADF, EOPNOTSUPP, ETXTBSY, EXDEV):
                        raise err

            # Use OS copy where available.
            if copyfd:
                copyfd(source_fd, target_fd)
                return
        except OSError as err:
            # Produce more useful error messages.
            err.filename = source_f.name
            err.filename2 = target_f.name
            raise err

    # Last resort: copy with fileobj read() and write().
    read_source = source_f.read
    write_target = target_f.write
    while buf := read_source(1024 * 1024):
        write_target(buf)


def get_file_metadata(path, follow_symlinks):
    if isinstance(path, os.DirEntry):
        st = path.stat(follow_symlinks=follow_symlinks)
    else:
        st = os.stat(path, follow_symlinks=follow_symlinks)
    result = {
        'mode': stat.S_IMODE(st.st_mode),
        'atime_ns': st.st_atime_ns,
        'mtime_ns': st.st_mtime_ns,
    }
    if hasattr(os, 'listxattr'):
        try:
            result['xattrs'] = [
                (attr, os.getxattr(path, attr, follow_symlinks=follow_symlinks))
                for attr in os.listxattr(path, follow_symlinks=follow_symlinks)]
        except OSError as err:
            if err.errno not in (EPERM, ENOTSUP, ENODATA, EINVAL, EACCES):
                raise
    if hasattr(st, 'st_flags'):
        result['flags'] = st.st_flags
    return result


def set_file_metadata(path, metadata, follow_symlinks):
    def _nop(*args, ns=None, follow_symlinks=None):
        pass

    if follow_symlinks:
        # use the real function if it exists
        def lookup(name):
            return getattr(os, name, _nop)
    else:
        # use the real function only if it exists
        # *and* it supports follow_symlinks
        def lookup(name):
            fn = getattr(os, name, _nop)
            if fn in os.supports_follow_symlinks:
                return fn
            return _nop

    lookup("utime")(path, ns=(metadata['atime_ns'], metadata['mtime_ns']),
                    follow_symlinks=follow_symlinks)
    # We must copy extended attributes before the file is (potentially)
    # chmod()'ed read-only, otherwise setxattr() will error with -EACCES.
    xattrs = metadata.get('xattrs')
    if xattrs is not None:
        for attr, value in xattrs:
            try:
                os.setxattr(path, attr, value, follow_symlinks=follow_symlinks)
            except OSError as e:
                if e.errno not in (EPERM, ENOTSUP, ENODATA, EINVAL, EACCES):
                    raise
    try:
        lookup("chmod")(path, metadata['mode'], follow_symlinks=follow_symlinks)
    except NotImplementedError:
        # if we got a NotImplementedError, it's because
        #   * follow_symlinks=False,
        #   * lchown() is unavailable, and
        #   * either
        #       * fchownat() is unavailable or
        #       * fchownat() doesn't implement AT_SYMLINK_NOFOLLOW.
        #         (it returned ENOSUP.)
        # therefore we're out of options--we simply cannot chown the
        # symlink.  give up, suppress the error.
        # (which is what shutil always did in this circumstance.)
        pass
    flags = metadata.get('flags')
    if flags is not None:
        try:
            lookup("chflags")(path, flags, follow_symlinks=follow_symlinks)
        except OSError as why:
            if why.errno not in (EOPNOTSUPP, ENOTSUP):
                raise
