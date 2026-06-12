"""Security and robustness tests for async_unzip.unzipper.

These cover the hardening added on top of the original extractor: path
traversal (Zip Slip) protection, encrypted-entry rejection, bounded
decompression with a declared-size guard, and the extracted-path return value.
"""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
import threading
import zipfile
import zlib
from pathlib import Path
from zipfile import BadZipFile

import pytest

from async_unzip import unzipper


def _run_with_timeout(coro_factory, timeout=15):
    """Run an async call in a daemon thread, failing if it never returns.

    The infinite-loop regression is a tight synchronous spin (no await on the
    empty-chunk path), so asyncio's own timeout cannot interrupt it; a watchdog
    thread lets the test fail cleanly instead of hanging the whole suite.
    """
    box = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(
            "extraction did not terminate within "
            f"{timeout}s (possible infinite loop)"
        )
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _infolist(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        return archive.infolist()


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


# --------------------------------------------------------------------------
# Integrity validation (size, CRC, eof) and the lying-compress_size DoS
# --------------------------------------------------------------------------


def test_unzip_terminates_on_oversized_compress_size(tmp_path, monkeypatch):
    # A crafted central directory that declares more compressed bytes than the
    # real deflate stream used to spin _drain forever once the output was
    # capped per block (small buffer) and a surplus chunk was fed in. The fix
    # stops at the deflate end-of-stream marker, so extraction must terminate
    # and still produce the correct bytes. A small buffer_size plus a second
    # entry (providing real trailing bytes) reproduces the original hang.
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "lie.zip"
    payload = b"A" * 2_000_000
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("victim.bin", payload)
        archive.writestr("filler.bin", b"B" * 500_000)

    infos = _infolist(archive_path)
    victim = [i for i in infos if i.filename == "victim.bin"]
    victim[0].compress_size += 200_000  # lie: declare a large surplus
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: victim)

    target = tmp_path / "out"
    _run_with_timeout(
        lambda: unzipper.unzip(
            str(archive_path), path=target, buffer_size=64
        ),
        timeout=15,
    )
    assert (target / "victim.bin").read_bytes() == payload


def test_write_compressed_entry_accepts_sync_flush_stream():
    # A deflate stream terminated with Z_SYNC_FLUSH (no final block) decodes
    # fully but never sets decomp.eof. stdlib accepts it when size and CRC
    # agree, so we must too instead of crying "truncated".
    payload = b"sync-flush payload " * 200
    compressor = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    data = compressor.compress(payload) + compressor.flush(zlib.Z_SYNC_FLUSH)
    stream = _AsyncChunkStream([data])
    out = _AsyncRecorder()
    unzipper._WINDOW_BITS_CACHE.clear()
    asyncio.run(
        unzipper._write_compressed_entry(
            stream,
            out,
            remaining=len(data),
            read_block=64,  # force per-block capping so eof never trips early
            file_name="sf",
            cache_key=None,
            error_types=(zlib.error,),
            factory=zlib.decompressobj,
            expected_size=len(payload),
            expected_crc=zlib.crc32(payload),
        )
    )
    assert bytes(out.data) == payload


def test_failed_crc_leaves_no_file_at_destination(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "crc.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"the original content")

    infos = _infolist(archive_path)
    infos[0].CRC ^= 0xFFFFFFFF
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    target = tmp_path / "out"
    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=target))

    # No corrupt file and no leftover .part temp at the destination.
    assert not (target / "a.txt").exists()
    assert list(target.glob("*.part")) == []
    assert list(target.glob("a.txt*")) == []


def test_unzip_rejects_zero_declared_size_inflation(tmp_path, monkeypatch):
    # A deflated entry that declares file_size=0 but inflates to real bytes
    # must be rejected (the `or None` hole used to disable the guard).
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "amp.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.bin", b"x" * 100_000)

    infos = _infolist(archive_path)
    infos[0].file_size = 0  # lie: claim empty
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(BadZipFile):
        _run_with_timeout(
            lambda: unzipper.unzip(str(archive_path), path=tmp_path / "out")
        )


def test_unzip_rejects_undersized_declared_size(tmp_path, monkeypatch):
    # Over-declared size (real output shorter than declared) is caught by the
    # final exact-size check.
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "under.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("f.bin", b"y" * 1000)

    infos = _infolist(archive_path)
    infos[0].file_size += 500  # claim more than the stream really yields
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))


def test_unzip_rejects_deflated_crc_mismatch(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "crc.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"the original content")

    infos = _infolist(archive_path)
    infos[0].CRC ^= 0xFFFFFFFF  # corrupt the declared CRC
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))


def test_unzip_rejects_stored_size_mismatch(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "stored.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("a.txt", b"hello stored world")

    infos = _infolist(archive_path)
    infos[0].file_size += 100  # disagree with compress_size
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))


def test_unzip_rejects_stored_crc_mismatch(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "stored_crc.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("a.txt", b"stored payload")

    infos = _infolist(archive_path)
    infos[0].CRC ^= 0xFFFFFFFF
    monkeypatch.setattr(unzipper, "_read_infolist", lambda _zf: infos)

    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))


@pytest.mark.parametrize(
    ("module", "method"),
    [("bz2", zipfile.ZIP_BZIP2), ("lzma", zipfile.ZIP_LZMA)],
)
def test_unzip_rejects_unsupported_compression(
    tmp_path, monkeypatch, module, method
):
    # Only stored/deflate are supported; other methods must raise a clear
    # NotImplementedError on the default path (not an opaque zlib error), and
    # the in-memory path must reject them too rather than silently decode.
    pytest.importorskip(module)
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "other.zip"
    with zipfile.ZipFile(archive_path, "w", method) as archive:
        archive.writestr("a.bin", b"payload" * 100)

    with pytest.raises(NotImplementedError):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path / "out"))

    async def chunks():
        yield archive_path.read_bytes()

    with pytest.raises(NotImplementedError):
        asyncio.run(
            unzipper.unzip_stream(
                chunks(), path=tmp_path / "mem", in_memory=True
            )
        )


def test_unzip_accepts_honest_empty_deflated_entry(tmp_path, monkeypatch):
    # Honest empty entries must still extract: compress_size > 0 so they take
    # the guarded path with expected_size == 0.
    _configure_async_reader(monkeypatch)
    archive_path = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("empty.txt", b"")

    target = tmp_path / "out"
    asyncio.run(unzipper.unzip(str(archive_path), path=target))
    assert (target / "empty.txt").read_bytes() == b""
