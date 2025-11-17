"""Async ZIP extraction helpers with minimal memory usage."""

import asyncio
import re
from pathlib import Path, PurePath
from typing import Iterable, Optional
from zipfile import ZIP_STORED, BadZipFile, ZipFile, is_zipfile
from zlib import MAX_WBITS
from zlib import decompressobj as _zlib_decompressobj
from zlib import error as ZLIB_error

try:  # pragma: no cover - optional dependency
    import uvloop
except ImportError:  # pragma: no cover
    uvloop = None
else:  # pragma: no cover
    try:
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:  # safety net; fallback silently
        pass

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

if _zlibng_module is not None:
    errors = (ZLIB_error,)
    if _ZLIBNG_ERROR is not None:
        errors += (_ZLIBNG_ERROR,)
    _AVAILABLE_BACKENDS["zlib-ng"] = {
        "factory": _zlibng_module.decompressobj,
        "errors": errors,
    }

if _isal_zlib is not None:
    errors = (ZLIB_error,)
    if _IsalError is not None:
        errors += (_IsalError,)
    _AVAILABLE_BACKENDS["python-isal"] = {
        "factory": _isal_zlib.decompressobj,
        "errors": errors,
    }

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
    print(  # pragma: no cover - mirrors legacy behaviour
        "Not aiofile nor aiofiles is present! Going to crash..\n"
        "    please do:\n"
        "        pip install aiofile\n"
        "    or\n"
        "        pip install aiofiles\n"
        "    to make the code working, Thanks!"
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


async def _write_stored_entry(src, out, remaining, read_block, file_name):
    """Stream an uncompressed entry out to disk."""
    while remaining > 0:
        chunk_size = read_block if remaining > read_block else remaining
        buf = await src.read(chunk_size)
        if not buf:
            raise BadZipFile(f"Incomplete stored entry for {file_name}")
        await out.write(buf)
        remaining -= len(buf)


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
        _WINDOW_BITS_CACHE[cache_key] = window_bits
    return window_bits


async def _write_compressed_entry(
    src,
    out,
    remaining,
    read_block,
    file_name,
    cache_key,
    error_types,
    factory,
    __debug=None,
):
    """Decompress a deflated entry while streaming to disk."""
    if remaining == 0:
        await out.write(b"")
        return

    first_chunk_size = read_block if remaining > read_block else remaining
    buf = await src.read(first_chunk_size)
    if not buf:
        raise BadZipFile(f"Incomplete compressed entry for {file_name}")
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

    while buf:
        await out.write(decomp.decompress(buf))
        if remaining <= 0:
            break
        chunk_size = read_block if remaining > read_block else remaining
        buf = await src.read(chunk_size)
        remaining -= len(buf)
        if not buf and remaining > 0:
            raise BadZipFile(f"Incomplete compressed entry for {file_name}")
        if __debug:
            print(f"Length: {len(buf)}")

    await out.write(decomp.flush())


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
    unpack_filename_path = Path(extra_path) / file_name
    if __debug:
        print(in_file)
        print(unpack_filename_path)

    if in_file.is_dir():
        unpack_filename_path.mkdir(parents=True, exist_ok=True)
        return

    parent_key = str(unpack_filename_path.parent)
    if parent_key not in created_dirs:
        unpack_filename_path.parent.mkdir(parents=True, exist_ok=True)
        created_dirs.add(parent_key)

    async with async_open(zip_path, mode="rb") as src:
        if async_reader == "aiofile":
            src.seek(in_file.header_offset)
        else:
            await src.seek(in_file.header_offset)
        if __debug:
            print(f"Done HEADER_OFFSET seek: {in_file.header_offset}")

        await _read_local_header(src, file_name, __debug=__debug)

        async with async_open(str(unpack_filename_path), "wb+") as out:
            remaining = in_file.compress_size
            read_block = _select_buffer_size(in_file.file_size, user_buffer)
            if in_file.compress_type == ZIP_STORED:
                await _write_stored_entry(
                    src,
                    out,
                    remaining,
                    read_block,
                    file_name,
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
                    __debug=__debug,
                )


async def unzip(  # pylint: disable=too-many-locals
    zip_file,
    path=None,
    files=None,
    regex_files=None,
    buffer_size=None,
    max_workers=4,
    backend=None,
    __debug=None,
):
    """Extract entries from a ZIP archive using async I/O."""
    user_buffer = buffer_size
    file_whitelist = set(files) if files else None
    regex_patterns = _compile_patterns(regex_files)

    if not is_zipfile(zip_file):
        raise BadZipFile

    if async_open is None:
        raise RuntimeError(
            "No async file backend available. Install aiofile or aiofiles."
        )

    backend_name, decompress_factory, error_types = _resolve_backend(backend)
    global DECOMPRESS_BACKEND, LAST_USED_BACKEND
    DECOMPRESS_BACKEND = backend_name
    LAST_USED_BACKEND = backend_name

    with ZipFile(zip_file) as archive:
        files_info = list(archive.infolist())
    extra_path = "" if path is None else PurePath(path)

    selected_entries = [
        info
        for info in files_info
        if _should_extract(info.filename, file_whitelist, regex_patterns)
    ]

    if not selected_entries:
        return

    worker_count = max(1, int(max_workers) if max_workers else 1)
    created_dirs = set()
    cache_key = f"{backend_name}:{zip_file}"
    try:
        asyncio.get_running_loop()
        semaphore = asyncio.Semaphore(worker_count)
    except RuntimeError:
        semaphore = None

    if semaphore is None or worker_count == 1 or len(selected_entries) == 1:
        for entry in selected_entries:
            await _extract_entry(
                zip_file,
                entry,
                extra_path,
                user_buffer,
                created_dirs,
                cache_key,
                error_types,
                decompress_factory,
                __debug,
            )
        return

    async def _bounded_extract(entry):
        async with semaphore:
            await _extract_entry(
                zip_file,
                entry,
                extra_path,
                user_buffer,
                created_dirs,
                cache_key,
                error_types,
                decompress_factory,
                __debug,
            )

    await asyncio.gather(
        *(_bounded_extract(entry) for entry in selected_entries)
    )
