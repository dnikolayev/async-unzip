"""Async ZIP extraction helpers with minimal memory usage."""

import asyncio
import atexit
import io
import logging
import os
import re
import tempfile
import warnings
from pathlib import Path, PurePath
from typing import AsyncIterable, Iterable, List, Optional
from zipfile import (
    ZIP_DEFLATED,
    ZIP_STORED,
    BadZipFile,
    ZipFile,
    is_zipfile,
)
from zlib import (
    MAX_WBITS,
    crc32 as _crc32,
    decompressobj as _zlib_decompressobj,
    error as ZLIB_error,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - optional dependency
    import uvloop
except ImportError:  # pragma: no cover
    uvloop = None

# Env var to opt out of the automatic uvloop policy (any non-empty value).
NO_UVLOOP_ENV = "ASYNC_UNZIP_NO_UVLOOP"


def _maybe_enable_uvloop():
    """Install uvloop's event-loop policy unless opted out or already set.

    Auto-enabling stays the default, but only when the host application has
    not already installed an event-loop policy and has not set
    ``ASYNC_UNZIP_NO_UVLOOP``. This avoids hijacking the process-wide policy
    out from under a caller that imports this library.
    """
    if uvloop is None:
        return
    if os.environ.get(NO_UVLOOP_ENV):
        return
    # Read the private sentinel rather than asyncio.get_event_loop_policy():
    # that getter instantiates the default policy as a side effect and is
    # deprecated on 3.12+. A missing attribute just means "go ahead".
    if getattr(asyncio.events, "_event_loop_policy", None) is not None:
        return
    try:
        with warnings.catch_warnings():
            # set_event_loop_policy is deprecated on 3.12+; we still support it.
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except (RuntimeError, ValueError):  # pragma: no cover - safety net
        pass


_maybe_enable_uvloop()

DEFAULT_READ_BUFFER_SIZE = 64 * 1024


def _select_buffer_size(entry_size, user_buffer):
    if user_buffer:
        size = int(user_buffer)
        return size if size > 0 else DEFAULT_READ_BUFFER_SIZE
    if entry_size < 1_000_000:
        return 32 * 1024
    if entry_size > 100_000_000:
        return 256 * 1024
    return DEFAULT_READ_BUFFER_SIZE


LOCAL_FILE_HEADER_SIZE = 30
LOCAL_FILE_HEADER_SIGNATURE = b"PK\x03\x04"
_WINDOW_BITS_CACHE = {}
# Cap the process-global window-bits cache so long-running services that
# extract many distinct archives do not grow it without bound.
_WINDOW_BITS_CACHE_MAX = 1024
# Bit 0 of the general purpose flag marks an entry as encrypted.
_ENCRYPTED_FLAG = 0x1
# Compression methods this extractor can decode (stored copy + deflate).
_SUPPORTED_COMPRESSION = frozenset({ZIP_STORED, ZIP_DEFLATED})


class LimitExceeded(Exception):
    """Raised when an archive exceeds a configured extraction limit.

    This is a policy refusal, not archive corruption (which raises
    :class:`zipfile.BadZipFile`), so callers can tell the two apart.
    """

    def __init__(self, limit, configured, observed, entry=None):
        self.limit = limit
        self.configured = configured
        self.observed = observed
        self.entry = entry
        message = f"{limit} exceeded: {observed} > {configured}"
        if entry is not None:
            message += f" (entry {entry!r})"
        super().__init__(message)


def _enforce_limits(
    entries,
    max_entries=None,
    max_entry_size=None,
    max_total_uncompressed_size=None,
):
    """Reject an entry set that breaches the configured size/count limits.

    Enforced from the central directory before any extraction. Because each
    entry is integrity-checked to produce exactly its declared uncompressed
    size, these declared totals bound what actually lands on disk.
    """
    if max_entries is not None and len(entries) > max_entries:
        raise LimitExceeded("max_entries", max_entries, len(entries))

    total = 0
    for entry in entries:
        size = entry.file_size
        if max_entry_size is not None and size > max_entry_size:
            raise LimitExceeded(
                "max_entry_size", max_entry_size, size, entry.filename
            )
        total += size
        if (
            max_total_uncompressed_size is not None
            and total > max_total_uncompressed_size
        ):
            raise LimitExceeded(
                "max_total_uncompressed_size",
                max_total_uncompressed_size,
                total,
            )


def _safe_destination(root, file_name):
    """Resolve *file_name* under *root*, rejecting path traversal.

    Guards against "Zip Slip": entries whose names contain ``..`` segments
    or absolute paths that would otherwise let a malicious archive write
    outside the extraction directory.
    """
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / file_name).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise BadZipFile(
            f"Unsafe entry name escapes extraction directory: {file_name!r}"
        )
    return candidate


def _ensure_supported(in_file):
    """Reject archive entries we cannot extract correctly."""
    if in_file.flag_bits & _ENCRYPTED_FLAG:
        raise NotImplementedError(
            f"Encrypted entries are not supported: {in_file.filename!r}"
        )
    if in_file.compress_type not in _SUPPORTED_COMPRESSION:
        raise NotImplementedError(
            f"Unsupported compression method {in_file.compress_type} "
            f"for {in_file.filename!r} (only stored and deflate are supported)"
        )


def _read_infolist(zip_file):
    """Validate the archive and read its central directory.

    Runs synchronously; callers offload it to a worker thread so the event
    loop is not blocked while the central directory is parsed.
    """
    if not is_zipfile(zip_file):
        raise BadZipFile
    with ZipFile(zip_file) as archive:
        return list(archive.infolist())


try:  # pragma: no cover - optional dependency
    from isal import isal_zlib as _isal_zlib
    from isal.isal_zlib import IsalError as _IsalError
except ImportError:  # pragma: no cover
    _isal_zlib = None
    _IsalError = None

try:  # pragma: no cover - optional dependency
    from zlib_ng import zlib_ng as _zlibng_module
except ImportError:  # pragma: no cover
    _zlibng_module = None
    _ZLIBNG_ERROR = None
else:  # pragma: no cover
    _ZLIBNG_ERROR = getattr(_zlibng_module, "error", None)

_AVAILABLE_BACKENDS = {
    "zlib": {
        "factory": _zlib_decompressobj,
        "errors": (ZLIB_error,),
    },
}


def _register_zlibng_backend():
    if _zlibng_module is None:
        return
    decompress = getattr(_zlibng_module, "decompressobj", None)
    if not decompress:
        return
    errors = (ZLIB_error,)
    if _ZLIBNG_ERROR is not None:
        errors += (_ZLIBNG_ERROR,)
    _AVAILABLE_BACKENDS["zlib-ng"] = {
        "factory": decompress,
        "errors": errors,
    }


def _register_isal_backend():
    if _isal_zlib is None:
        return
    decompress = getattr(_isal_zlib, "decompressobj", None)
    if not decompress:
        return
    errors = (ZLIB_error,)
    if _IsalError is not None:
        errors += (_IsalError,)
    _AVAILABLE_BACKENDS["python-isal"] = {
        "factory": decompress,
        "errors": errors,
    }


_register_zlibng_backend()
_register_isal_backend()

DEFAULT_BACKEND = "zlib"
DECOMPRESS_BACKEND = DEFAULT_BACKEND  # last used backend
AVAILABLE_BACKENDS = tuple(_AVAILABLE_BACKENDS.keys())

try:
    from aiofile import async_open as _AIOFILE_OPEN
except ModuleNotFoundError:  # pragma: no cover - platform specific
    _AIOFILE_OPEN = None

try:
    from aiofiles import open as _AIOFILES_OPEN
except (ModuleNotFoundError, ImportError):  # pragma: no cover
    _AIOFILES_OPEN = None

MISSED_MODULES = int(_AIOFILE_OPEN is None) + int(_AIOFILES_OPEN is None)

if _AIOFILES_OPEN:
    ASYNC_READER = "aiofiles"
    ASYNC_OPEN = _AIOFILES_OPEN
elif _AIOFILE_OPEN:
    ASYNC_READER = "aiofile"
    ASYNC_OPEN = _AIOFILE_OPEN
else:
    ASYNC_READER = "aiofile"
    ASYNC_OPEN = None
    logger.warning(  # pragma: no cover - mirrors legacy behaviour
        "Neither aiofile nor aiofiles is installed; async I/O is unavailable. "
        "Install one with `pip install aiofile` or `pip install aiofiles`."
    )

# Backwards compatibility for external imports.
async_open = ASYNC_OPEN
async_reader = ASYNC_READER  # pylint: disable=invalid-name
missed_modules = MISSED_MODULES
LAST_USED_BACKEND = DEFAULT_BACKEND


def _resolve_backend(name):
    backend_name = (name or DEFAULT_BACKEND).lower()
    if backend_name not in _AVAILABLE_BACKENDS:
        raise ValueError(
            f"Unknown backend '{backend_name}'. "
            f"Available: {', '.join(AVAILABLE_BACKENDS)}"
        )
    factory = _AVAILABLE_BACKENDS[backend_name]["factory"]
    errors = _AVAILABLE_BACKENDS[backend_name]["errors"]
    return backend_name, factory, errors


def _compile_patterns(regex_files: Optional[Iterable[str]]):
    """Compile optional regex filters."""
    if not regex_files:
        return None
    if isinstance(regex_files, (list, tuple)):
        regex_list = list(regex_files)
    else:
        regex_list = [regex_files]
    return [re.compile(pattern) for pattern in regex_list]


def _should_extract(file_name, whitelist, regex_patterns):
    """Return True when the entry should be extracted."""
    matches_whitelist = not whitelist or file_name in whitelist
    matches_regex = not regex_patterns or any(
        pattern.search(file_name) for pattern in regex_patterns
    )
    return matches_whitelist and matches_regex


async def _read_local_header(src, file_name, __debug=None):
    """Read the local header and skip filename/extra blocks."""
    header = await src.read(LOCAL_FILE_HEADER_SIZE)
    if (
        len(header) != LOCAL_FILE_HEADER_SIZE
        or header[:4] != LOCAL_FILE_HEADER_SIGNATURE
    ):
        raise BadZipFile(f"Invalid local header for {file_name}")

    name_length = int.from_bytes(header[26:28], "little")
    extra_length = int.from_bytes(header[28:30], "little")
    if __debug:
        print(f"Done FILEPATH seek: {LOCAL_FILE_HEADER_SIZE} - {header}")
    if name_length:
        filename_bytes = await src.read(name_length)
        if __debug:
            print(f"Done FILENAME seek: {name_length} - {filename_bytes}")
    if extra_length:
        extra_bytes = await src.read(extra_length)
        if __debug:
            print(f"Done EXTRA seek: {extra_length} {extra_bytes}")


async def _write_stored_entry(
    src, out, remaining, read_block, file_name, expected_crc=None
):
    """Stream an uncompressed entry out to disk, verifying its CRC-32."""
    running_crc = 0
    while remaining > 0:
        chunk_size = read_block if remaining > read_block else remaining
        buf = await src.read(chunk_size)
        if not buf:
            raise BadZipFile(f"Incomplete stored entry for {file_name}")
        await out.write(buf)
        running_crc = _crc32(buf, running_crc)
        remaining -= len(buf)
    if expected_crc is not None and running_crc != expected_crc:
        raise BadZipFile(f"Bad CRC-32 for file {file_name!r}")


async def _probe_window_bits(buf, error_types, factory, __debug=None):
    """Auto-detect window bits for compressed payloads."""
    for window_bits in (-MAX_WBITS, MAX_WBITS | 16, MAX_WBITS):
        try:
            factory(window_bits).decompress(buf)
            if __debug:
                print(f"Try WindowBits: {window_bits}")
            return window_bits
        except error_types:
            if __debug:
                print(f"Failed WindowBits: {window_bits}")
            continue
    raise ZLIB_error("Unable to detect compression window size")


async def _detect_window_bits(
    buf,
    error_types,
    factory,
    cache_key=None,
    __debug=None,
):
    """Return cached window bits or probe and cache the result."""
    if cache_key:
        cached = _WINDOW_BITS_CACHE.get(cache_key)
        if cached is not None:
            return cached

    window_bits = await _probe_window_bits(
        buf,
        error_types,
        factory,
        __debug=__debug,
    )
    if cache_key:
        if (
            len(_WINDOW_BITS_CACHE) >= _WINDOW_BITS_CACHE_MAX
            and cache_key not in _WINDOW_BITS_CACHE
        ):
            # Evict the oldest entry (dicts preserve insertion order).
            _WINDOW_BITS_CACHE.pop(next(iter(_WINDOW_BITS_CACHE)))
        _WINDOW_BITS_CACHE[cache_key] = window_bits
    return window_bits


# pylint: disable=too-many-arguments
# pylint: disable=too-many-positional-arguments
async def _write_compressed_entry(
    src,
    out,
    remaining,
    read_block,
    file_name,
    cache_key,
    error_types,
    factory,
    expected_size=None,
    expected_crc=None,
    __debug=None,
):
    """Decompress a deflated entry while streaming to disk.

    Output is flushed in ``read_block`` slices to cap peak memory (the
    ``zlib``/``zlib-ng`` backends honour the per-call bound; ``python-isal``
    caps each returned chunk but releases buffered output via ``flush()``).

    Integrity is validated like the stdlib reader: decompression stops as soon
    as the deflate end-of-stream marker is reached (so a declared
    ``compress_size`` larger than the real stream cannot spin forever), the
    stream must actually reach that marker, the decompressed length must equal
    the declared ``expected_size``, and the running CRC-32 must match
    ``expected_crc``. These catch corrupt, truncated, or size-/CRC-spoofed
    entries; they are a consistency check, not a substitute for explicit
    resource limits.
    """
    if remaining == 0:
        # No compressed payload: only consistent with an empty entry.
        if expected_size:
            raise BadZipFile(f"Truncated compressed entry for {file_name}")
        await out.write(b"")
        if expected_crc:
            raise BadZipFile(f"Bad CRC-32 for file {file_name!r}")
        return

    first_chunk_size = read_block if remaining > read_block else remaining
    buf = await src.read(first_chunk_size)
    if not buf:
        raise BadZipFile(
            f"Incomplete compressed entry for {file_name}"
        )
    remaining -= len(buf)

    window_bits = await _detect_window_bits(
        buf,
        error_types,
        factory,
        cache_key=cache_key,
        __debug=__debug,
    )
    decomp = factory(window_bits)
    if __debug:
        print(f"Incoming Length: {len(buf)}")

    produced = 0
    running_crc = 0

    async def _drain(data):
        nonlocal produced, running_crc
        # Stop once the deflate stream ends; any trailing input is surplus
        # from a lying compress_size and must not be fed back in (that spins
        # forever on unconsumed_tail).
        while data and not decomp.eof:
            chunk = decomp.decompress(data, read_block)
            if chunk:
                produced += len(chunk)
                if expected_size is not None and produced > expected_size:
                    raise BadZipFile(
                        "Decompressed size exceeds declared size for "
                        f"{file_name}"
                    )
                running_crc = _crc32(chunk, running_crc)
                await out.write(chunk)
            data = decomp.unconsumed_tail

    while buf:
        await _drain(buf)
        if decomp.eof or remaining <= 0:
            break
        chunk_size = read_block if remaining > read_block else remaining
        buf = await src.read(chunk_size)
        remaining -= len(buf)
        if not buf and remaining > 0:
            raise BadZipFile(
                f"Incomplete compressed entry for {file_name}"
            )
        if __debug:
            print(f"Length: {len(buf)}")

    tail = decomp.flush()
    if tail:
        produced += len(tail)
        if expected_size is not None and produced > expected_size:
            raise BadZipFile(
                f"Decompressed size exceeds declared size for {file_name}"
            )
        running_crc = _crc32(tail, running_crc)
        await out.write(tail)

    # Size and CRC are the authoritative integrity gates.
    if expected_size is not None and produced != expected_size:
        raise BadZipFile(f"Decompressed size mismatch for {file_name}")
    if expected_crc is not None and running_crc != expected_crc:
        raise BadZipFile(f"Bad CRC-32 for file {file_name!r}")
    # A missing deflate end-of-stream marker only signals truncation when the
    # size/CRC evidence cannot vouch for the output. Streams flushed with
    # Z_SYNC_FLUSH decode fully yet never set eof; stdlib accepts them as long
    # as the declared size and CRC check out, so we do too.
    if not decomp.eof and (expected_size is None or expected_crc is None):
        raise BadZipFile(f"Truncated compressed entry for {file_name}")


async def _extract_entry(  # pylint: disable=too-many-arguments
    zip_path,
    in_file,
    extra_path,
    user_buffer,
    created_dirs,
    cache_key,
    error_types,
    factory,
    __debug,
):
    file_name = in_file.filename
    unpack_filename_path = _safe_destination(extra_path, file_name)
    if __debug:
        print(in_file)
        print(unpack_filename_path)

    if in_file.is_dir():
        unpack_filename_path.mkdir(parents=True, exist_ok=True)
        return unpack_filename_path

    _ensure_supported(in_file)

    # A stored entry is copied verbatim, so its declared sizes must agree;
    # a mismatch means a corrupt central directory. Checked before any file
    # is created so a bad entry leaves nothing behind.
    if (
        in_file.compress_type == ZIP_STORED
        and in_file.file_size != in_file.compress_size
    ):
        raise BadZipFile(f"Stored entry size mismatch for {file_name}")

    parent = unpack_filename_path.parent
    parent_key = str(parent)
    if parent_key not in created_dirs:
        parent.mkdir(parents=True, exist_ok=True)
        created_dirs.add(parent_key)

    # Extract atomically: stream into a temporary file in the same directory
    # and move it into place only after every integrity check passes, so a
    # failed CRC/size check never leaves a corrupt file at the destination.
    handle, tmp_name = tempfile.mkstemp(
        prefix=f"{unpack_filename_path.name}.", suffix=".part", dir=str(parent)
    )
    os.close(handle)
    tmp_path = Path(tmp_name)
    try:
        async with async_open(zip_path, mode="rb") as src:
            if async_reader == "aiofile":
                src.seek(in_file.header_offset)
            else:
                await src.seek(in_file.header_offset)
            if __debug:
                print(f"Done HEADER_OFFSET seek: {in_file.header_offset}")

            await _read_local_header(src, file_name, __debug=__debug)

            async with async_open(str(tmp_path), "wb+") as out:
                remaining = in_file.compress_size
                read_block = _select_buffer_size(
                    in_file.file_size, user_buffer
                )
                if in_file.compress_type == ZIP_STORED:
                    await _write_stored_entry(
                        src,
                        out,
                        remaining,
                        read_block,
                        file_name,
                        expected_crc=in_file.CRC,
                    )
                else:
                    await _write_compressed_entry(
                        src,
                        out,
                        remaining,
                        read_block,
                        file_name,
                        cache_key=cache_key,
                        error_types=error_types,
                        factory=factory,
                        expected_size=in_file.file_size,
                        expected_crc=in_file.CRC,
                        __debug=__debug,
                    )
        os.replace(tmp_path, unpack_filename_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return unpack_filename_path


async def unzip(  # pylint: disable=too-many-locals,too-many-arguments
    zip_file,
    path=None,
    files=None,
    regex_files=None,
    buffer_size=None,
    max_workers=4,
    backend=None,
    __debug=None,
    *,
    max_entries=None,
    max_entry_size=None,
    max_total_uncompressed_size=None,
):
    """Extract entries from a ZIP archive using async I/O.

    Returns the list of paths written to disk (files and directory entries).

    Optional resource limits (keyword-only, ``None`` means unlimited) are
    checked against the central directory before extraction and raise
    :class:`LimitExceeded` on breach:

    - ``max_entries``: maximum number of selected members (files and dirs).
    - ``max_entry_size``: maximum uncompressed size of any single entry.
    - ``max_total_uncompressed_size``: maximum total uncompressed size.
    """
    user_buffer = buffer_size
    file_whitelist = set(files) if files else None
    regex_patterns = _compile_patterns(regex_files)

    if async_open is None:
        raise RuntimeError(
            "No async file backend available. Install aiofile or aiofiles."
        )

    backend_name, decompress_factory, error_types = _resolve_backend(backend)
    globals()["DECOMPRESS_BACKEND"] = backend_name
    globals()["LAST_USED_BACKEND"] = backend_name

    files_info = await asyncio.to_thread(_read_infolist, zip_file)
    extra_path = "" if path is None else PurePath(path)

    selected_entries = [
        info
        for info in files_info
        if _should_extract(info.filename, file_whitelist, regex_patterns)
    ]

    if not selected_entries:
        return []

    _enforce_limits(
        selected_entries,
        max_entries=max_entries,
        max_entry_size=max_entry_size,
        max_total_uncompressed_size=max_total_uncompressed_size,
    )

    worker_count = max(1, int(max_workers) if max_workers else 1)
    created_dirs = set()
    cache_key_base = f"{backend_name}:{zip_file}"
    try:
        asyncio.get_running_loop()
        semaphore = asyncio.Semaphore(worker_count)
    except RuntimeError:
        semaphore = None

    if semaphore is None or worker_count == 1 or len(selected_entries) == 1:
        written = []
        for entry in selected_entries:
            entry_cache = f"{cache_key_base}:{entry.filename}"
            written.append(
                await _extract_entry(
                    zip_file,
                    entry,
                    extra_path,
                    user_buffer,
                    created_dirs,
                    entry_cache,
                    error_types,
                    decompress_factory,
                    __debug,
                )
            )
        return [item for item in written if item is not None]

    async def _bounded_extract(entry):
        async with semaphore:
            entry_cache = f"{cache_key_base}:{entry.filename}"
            return await _extract_entry(
                zip_file,
                entry,
                extra_path,
                user_buffer,
                created_dirs,
                entry_cache,
                error_types,
                decompress_factory,
                __debug,
            )

    results = await asyncio.gather(
        *(_bounded_extract(entry) for entry in selected_entries)
    )
    return [item for item in results if item is not None]


# pylint: disable=too-many-locals
# pylint: disable=too-many-arguments
# pylint: disable=too-many-positional-arguments
# pylint: disable=too-many-statements
async def unzip_stream(
    chunk_iterable: AsyncIterable[bytes],
    path=None,
    files=None,
    regex_files=None,
    buffer_size=None,
    max_workers=4,
    backend=None,
    spool_dir=None,
    in_memory: bool = False,
    __debug=None,
    *,
    max_entries=None,
    max_entry_size=None,
    max_total_uncompressed_size=None,
    max_archive_size=None,
):
    """Extract a ZIP archive provided as an async stream of chunks.

    The incoming chunks are spooled to a temporary file (optionally inside
    ``spool_dir``) and then processed via :func:`unzip`. Supply the same
    filtering arguments as :func:`unzip` to limit extracted entries. Returns
    the list of paths written to disk.

    The ``max_entries``/``max_entry_size``/``max_total_uncompressed_size``
    limits behave as in :func:`unzip`. ``max_archive_size`` additionally caps
    the number of raw bytes consumed from the stream (the spooled/buffered
    archive itself), raising :class:`LimitExceeded` before an oversized
    download is fully read.

    .. deprecated::
        ``in_memory=True`` is deprecated and will be removed in a future
        release. It buffers the whole archive in RAM, decompresses each entry
        synchronously via the stdlib ``zipfile`` reader (blocking the event
        loop), and ignores ``backend`` and ``max_workers``. Use the default
        spooled path instead; point ``spool_dir`` at any writable location to
        control where the temporary archive is stored.
    """

    if chunk_iterable is None or not hasattr(chunk_iterable, "__aiter__"):
        raise TypeError(
            "chunk_iterable must be an AsyncIterable yielding "
            "bytes-like chunks"
        )

    if async_open is None:
        raise RuntimeError(
            "No async file backend available. Install aiofile or aiofiles."
        )

    spool_parent = (
        Path(spool_dir) if spool_dir else Path(tempfile.gettempdir())
    )
    spool_parent.mkdir(parents=True, exist_ok=True)

    def _check_archive_size(consumed):
        if max_archive_size is not None and consumed > max_archive_size:
            raise LimitExceeded(
                "max_archive_size", max_archive_size, consumed
            )

    async def _iter_chunks_to_buffer() -> io.BytesIO:
        buf = io.BytesIO()
        consumed = 0
        async for chunk in chunk_iterable:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("chunk_iterable must yield bytes-like objects")
            if not chunk:
                continue
            consumed += len(chunk)
            _check_archive_size(consumed)
            buf.write(bytes(chunk))
        buf.seek(0)
        return buf

    async def _extract_from_buffer(buf: io.BytesIO) -> List[Path]:
        file_whitelist = set(files) if files else None
        regex_patterns = _compile_patterns(regex_files)
        extra_path = "" if path is None else PurePath(path)
        written: List[Path] = []

        with ZipFile(buf) as archive:
            selected_entries = [
                info
                for info in archive.infolist()
                if _should_extract(
                    info.filename,
                    file_whitelist,
                    regex_patterns,
                )
            ]

            if not selected_entries:
                return written

            _enforce_limits(
                selected_entries,
                max_entries=max_entries,
                max_entry_size=max_entry_size,
                max_total_uncompressed_size=max_total_uncompressed_size,
            )

            created_dirs: set = set()
            for entry in selected_entries:
                file_name = entry.filename
                unpack_filename_path = _safe_destination(extra_path, file_name)

                if entry.is_dir():
                    unpack_filename_path.mkdir(parents=True, exist_ok=True)
                    written.append(unpack_filename_path)
                    continue

                _ensure_supported(entry)

                parent_key = str(unpack_filename_path.parent)
                if parent_key not in created_dirs:
                    unpack_filename_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    created_dirs.add(parent_key)

                read_block = _select_buffer_size(entry.file_size, buffer_size)
                # The stdlib reader verifies the CRC as it goes; extract into a
                # temp file and move it into place only on success so a corrupt
                # entry never leaves a partial file at the destination.
                handle, tmp_name = tempfile.mkstemp(
                    prefix=f"{unpack_filename_path.name}.",
                    suffix=".part",
                    dir=str(unpack_filename_path.parent),
                )
                os.close(handle)
                tmp_path = Path(tmp_name)
                try:
                    with archive.open(entry) as src:
                        async with async_open(str(tmp_path), "wb+") as out:
                            while True:
                                chunk = src.read(read_block)
                                if not chunk:
                                    break
                                await out.write(chunk)
                    os.replace(tmp_path, unpack_filename_path)
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
                written.append(unpack_filename_path)
        return written

    if in_memory:
        warnings.warn(
            "unzip_stream(in_memory=True) is deprecated and will be removed "
            "in a future release: it buffers the whole archive in RAM, "
            "decompresses synchronously, and ignores the backend/max_workers "
            "options. Use the default spooled path (optionally with "
            "spool_dir) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        buffer = await _iter_chunks_to_buffer()
        return await _extract_from_buffer(buffer)

    cleanup_handle = None
    temp_path: Optional[Path] = None

    try:
        fd, temp_name = tempfile.mkstemp(suffix=".zip", dir=str(spool_parent))
        os.close(fd)
        temp_path = Path(temp_name)

        def _cleanup(path=temp_path):
            try:
                path.unlink(missing_ok=True)
            except FileNotFoundError:  # pragma: no cover - already removed
                pass

        cleanup_handle = _cleanup
        atexit.register(cleanup_handle)

        try:
            consumed = 0
            async with async_open(str(temp_path), "wb") as temp_file:
                async for chunk in chunk_iterable:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise TypeError(
                            "chunk_iterable must yield bytes-like objects"
                        )
                    if not chunk:
                        continue
                    consumed += len(chunk)
                    _check_archive_size(consumed)
                    await temp_file.write(bytes(chunk))
        finally:
            if cleanup_handle:
                atexit.unregister(cleanup_handle)

        return await unzip(
            str(temp_path),
            path=path,
            files=files,
            regex_files=regex_files,
            buffer_size=buffer_size,
            max_workers=max_workers,
            backend=backend,
            max_entries=max_entries,
            max_entry_size=max_entry_size,
            max_total_uncompressed_size=max_total_uncompressed_size,
            __debug=__debug,
        )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
