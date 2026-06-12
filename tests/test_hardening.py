"""Security and robustness tests for async_unzip.unzipper.

These cover the hardening added on top of the original extractor: path
traversal (Zip Slip) protection, encrypted-entry rejection, bounded
decompression with a declared-size guard, and the extracted-path return value.
"""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
import zipfile
import zlib
from pathlib import Path
from zipfile import BadZipFile

import pytest

from async_unzip import unzipper

BACKENDS = ["zlib"]
try:  # optional accelerators, exercised when installed
    import zlib_ng  # noqa: F401

    BACKENDS.append("zlib-ng")
except ImportError:  # pragma: no cover - depends on environment
    pass
try:
    import isal  # noqa: F401

    BACKENDS.append("python-isal")
except ImportError:  # pragma: no cover - depends on environment
    pass


class _AsyncFile:
    def __init__(self, fp):
        self._fp = fp

    async def read(self, size=-1):
        return self._fp.read(size)

    async def write(self, data):
        return self._fp.write(data)

    async def seek(self, offset, whence=0):
        self._fp.seek(offset, whence)
        return self._fp.tell()


class _AsyncOpen:
    def __init__(self, path, mode):
        self._path = path
        self._mode = mode
        self._fp = None

    async def __aenter__(self):
        self._fp = open(  # pylint: disable=consider-using-with
            self._path, self._mode
        )
        return _AsyncFile(self._fp)

    async def __aexit__(self, exc_type, exc, tb):
        if self._fp:
            self._fp.close()


def _configure_async_reader(monkeypatch):
    def _async_open(path, mode="rb"):
        return _AsyncOpen(path, mode)

    monkeypatch.setattr(unzipper, "async_reader", "aiofiles", raising=False)
    monkeypatch.setattr(unzipper, "async_open", _async_open, raising=False)


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _AsyncRecorder:
    def __init__(self):
        self.data = bytearray()

    async def write(self, payload):
        self.data.extend(payload)


# --------------------------------------------------------------------------
# Zip Slip / path traversal
# --------------------------------------------------------------------------


def test_safe_destination_allows_nested_relative(tmp_path):
    root = tmp_path / "out"
    resolved = unzipper._safe_destination(root, "a/b/c.txt")
    assert resolved == (root / "a" / "b" / "c.txt").resolve()


@pytest.mark.parametrize(
    "evil_name",
    [
        "../escape.txt",
        "../../escape.txt",
        "a/../../escape.txt",
        "/abs/escape.txt",
    ],
)
def test_safe_destination_rejects_escapes(tmp_path, evil_name):
    root = tmp_path / "out"
    with pytest.raises(BadZipFile):
        unzipper._safe_destination(root, evil_name)


def test_unzip_rejects_parent_traversal(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "evil.zip"
    info = zipfile.ZipInfo("../escape.txt")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(info, b"pwned")

    target = tmp_path / "out"
    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=target))

    # The malicious entry must not have been written outside the target.
    assert not (tmp_path / "escape.txt").exists()


def test_unzip_stream_in_memory_rejects_traversal(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "evil.zip"
    info = zipfile.ZipInfo("../escape.txt")
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(info, b"pwned")

    async def chunks():
        yield archive_path.read_bytes()

    with pytest.raises(BadZipFile):
        asyncio.run(
            unzipper.unzip_stream(
                chunks(), path=tmp_path / "out", in_memory=True
            )
        )
    assert not (tmp_path / "escape.txt").exists()


# --------------------------------------------------------------------------
# Encrypted entries
# --------------------------------------------------------------------------


def test_ensure_supported_rejects_encrypted_flag():
    info = zipfile.ZipInfo("secret.txt")
    info.flag_bits |= 0x1
    with pytest.raises(NotImplementedError):
        unzipper._ensure_supported(info)
    # A normal entry passes through untouched.
    unzipper._ensure_supported(zipfile.ZipInfo("plain.txt"))


def test_unzip_rejects_encrypted_entry(tmp_path, monkeypatch):
    # stdlib zipfile recomputes flag_bits on write, so build a normal
    # archive and tamper the in-memory infolist to look encrypted.
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "enc.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("secret.txt", b"classified")

    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
    infos[0].flag_bits |= 0x1
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(NotImplementedError):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))


# --------------------------------------------------------------------------
# Bounded decompression + declared-size guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_bounded_decompression_large_entry(tmp_path, monkeypatch, backend):
    _configure_async_reader(monkeypatch)
    payload = (b"abcdefghij" * 1024) * 256  # ~2.5 MB, compressible
    archive_path = tmp_path / "big.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.bin", payload)

    target = tmp_path / "out"
    # Tiny buffer forces many read blocks and unconsumed_tail draining.
    asyncio.run(
        unzipper.unzip(
            str(archive_path),
            path=target,
            buffer_size=4096,
            backend=backend,
        )
    )
    assert (target / "big.bin").read_bytes() == payload


def test_write_compressed_entry_enforces_declared_size():
    payload = b"A" * 5000
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(payload) + compressor.flush()

    stream = _AsyncChunkStream([compressed])
    out = _AsyncRecorder()
    with pytest.raises(BadZipFile):
        asyncio.run(
            unzipper._write_compressed_entry(
                stream,
                out,
                remaining=len(compressed),
                read_block=64,
                file_name="bomb",
                cache_key=None,
                error_types=(zlib.error,),
                factory=zlib.decompressobj,
                expected_size=100,  # actual inflates to 5000
            )
        )


def test_write_compressed_entry_roundtrip_within_declared_size():
    payload = b"A" * 5000
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = compressor.compress(payload) + compressor.flush()

    stream = _AsyncChunkStream([compressed])
    out = _AsyncRecorder()
    asyncio.run(
        unzipper._write_compressed_entry(
            stream,
            out,
            remaining=len(compressed),
            read_block=64,
            file_name="ok",
            cache_key=None,
            error_types=(zlib.error,),
            factory=zlib.decompressobj,
            expected_size=len(payload),
        )
    )
    assert bytes(out.data) == payload


# --------------------------------------------------------------------------
# Return value
# --------------------------------------------------------------------------


def test_unzip_returns_extracted_paths(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "data.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"alpha")
        archive.writestr("nested/b.txt", b"beta")

    target = tmp_path / "out"
    written = asyncio.run(unzipper.unzip(str(archive_path), path=target))

    assert {Path(p).name for p in written} >= {"a.txt", "b.txt"}
    for item in written:
        assert Path(item).exists()


def test_unzip_returns_empty_list_when_nothing_matches(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "data.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"alpha")

    result = asyncio.run(
        unzipper.unzip(
            str(archive_path),
            path=tmp_path / "out",
            files=["does-not-exist"],
        )
    )
    assert result == []


# --------------------------------------------------------------------------
# Window-bits cache bound
# --------------------------------------------------------------------------


def test_window_bits_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(unzipper, "_WINDOW_BITS_CACHE_MAX", 8)
    unzipper._WINDOW_BITS_CACHE.clear()

    async def fake_probe(buf, error_types, factory, __debug=None):
        return -zlib.MAX_WBITS

    monkeypatch.setattr(unzipper, "_probe_window_bits", fake_probe)

    async def drive():
        for idx in range(50):
            await unzipper._detect_window_bits(
                b"x",
                (zlib.error,),
                zlib.decompressobj,
                cache_key=f"key-{idx}",
            )

    asyncio.run(drive())
    assert len(unzipper._WINDOW_BITS_CACHE) <= 8
