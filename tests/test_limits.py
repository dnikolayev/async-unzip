"""Resource-limit tests for async_unzip.unzipper (0.7.0)."""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
import zipfile
from pathlib import Path

import pytest

from async_unzip import LimitExceeded, unzipper


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
        self._fp = open(self._path, self._mode)  # pylint: disable=consider-using-with
        return _AsyncFile(self._fp)

    async def __aexit__(self, exc_type, exc, tb):
        if self._fp:
            self._fp.close()


@pytest.fixture
def async_reader(monkeypatch):
    def _async_open(path, mode="rb"):
        return _AsyncOpen(path, mode)

    monkeypatch.setattr(unzipper, "async_reader", "aiofiles", raising=False)
    monkeypatch.setattr(unzipper, "async_open", _async_open, raising=False)


def _make_archive(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


# --------------------------------------------------------------------------
# Public exception surface
# --------------------------------------------------------------------------


def test_limit_exceeded_is_not_badzipfile():
    # Policy refusal must be distinguishable from archive corruption.
    assert not issubclass(LimitExceeded, zipfile.BadZipFile)
    exc = LimitExceeded("max_entries", 2, 5)
    assert exc.limit == "max_entries"
    assert exc.configured == 2
    assert exc.observed == 5
    assert "max_entries" in str(exc)


# --------------------------------------------------------------------------
# unzip() limits
# --------------------------------------------------------------------------


def test_unzip_max_entries(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("a.txt", b"a"), ("b.txt", b"b"), ("c.txt", b"c")],
    )
    with pytest.raises(LimitExceeded) as info:
        asyncio.run(unzipper.unzip(str(archive), path=tmp_path / "out", max_entries=2))
    assert info.value.limit == "max_entries"
    # Fail-fast: nothing extracted.
    assert not (tmp_path / "out").exists()


def test_unzip_max_entry_size(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("small.txt", b"x" * 10), ("big.txt", b"y" * 5000)],
    )
    with pytest.raises(LimitExceeded) as info:
        asyncio.run(
            unzipper.unzip(str(archive), path=tmp_path / "out", max_entry_size=1000)
        )
    assert info.value.limit == "max_entry_size"
    assert info.value.entry == "big.txt"


def test_unzip_max_total_uncompressed_size(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("a.txt", b"x" * 600), ("b.txt", b"y" * 600)],
    )
    with pytest.raises(LimitExceeded) as info:
        asyncio.run(
            unzipper.unzip(
                str(archive),
                path=tmp_path / "out",
                max_total_uncompressed_size=1000,
            )
        )
    assert info.value.limit == "max_total_uncompressed_size"


def test_unzip_within_limits_succeeds(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("a.txt", b"x" * 100), ("b.txt", b"y" * 100)],
    )
    target = tmp_path / "out"
    written = asyncio.run(
        unzipper.unzip(
            str(archive),
            path=target,
            max_entries=10,
            max_entry_size=1000,
            max_total_uncompressed_size=1000,
        )
    )
    assert len(written) == 2
    assert (target / "a.txt").read_bytes() == b"x" * 100


def test_unzip_limits_are_keyword_only(tmp_path, async_reader):
    archive = _make_archive(tmp_path / "a.zip", [("a.txt", b"a")])
    with pytest.raises(TypeError):
        # The 9th positional overflows past zip_file..backend, __debug into the
        # keyword-only limits, so limits cannot be passed positionally.
        asyncio.run(
            unzipper.unzip(
                str(archive), None, None, None, None, 4, None, None, 1  # noqa
            )
        )


def test_unzip_debug_stays_positional(tmp_path, async_reader):
    # __debug remains positional-or-keyword (back-compat) even after adding the
    # keyword-only limits.
    archive = _make_archive(tmp_path / "a.zip", [("a.txt", b"hi")])
    target = tmp_path / "out"
    # 8th positional binds to __debug without error.
    written = asyncio.run(
        unzipper.unzip(str(archive), target, None, None, None, 4, None, False)
    )
    assert len(written) == 1


def test_unzip_limits_count_filtered_entries(tmp_path, async_reader):
    # Limits apply to the SELECTED subset, not the whole archive.
    archive = _make_archive(
        tmp_path / "a.zip",
        [("keep.txt", b"x" * 100), ("drop.txt", b"y" * 100000)],
    )
    target = tmp_path / "out"
    written = asyncio.run(
        unzipper.unzip(
            str(archive),
            path=target,
            files=["keep.txt"],
            max_entry_size=1000,  # would trip on drop.txt if not filtered
        )
    )
    assert {Path(p).name for p in written} == {"keep.txt"}


# --------------------------------------------------------------------------
# unzip_stream() limits
# --------------------------------------------------------------------------


def _chunks_of(path, step=4096):
    async def _gen():
        data = Path(path).read_bytes()
        for idx in range(0, len(data), step):
            yield data[idx : idx + step]

    return _gen()


def test_unzip_stream_spooled_enforces_entry_limits(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("a.txt", b"x" * 100), ("b.txt", b"y" * 100)],
    )
    with pytest.raises(LimitExceeded):
        asyncio.run(
            unzipper.unzip_stream(
                _chunks_of(archive),
                path=tmp_path / "out",
                spool_dir=tmp_path / "spool",
                max_entries=1,
            )
        )


def test_unzip_stream_in_memory_enforces_entry_limits(tmp_path, async_reader):
    archive = _make_archive(
        tmp_path / "a.zip",
        [("a.txt", b"x" * 100), ("b.txt", b"y" * 100)],
    )
    with pytest.raises(LimitExceeded):
        asyncio.run(
            unzipper.unzip_stream(
                _chunks_of(archive),
                path=tmp_path / "out",
                spool_dir=tmp_path / "spool",
                in_memory=True,
                max_total_uncompressed_size=150,
            )
        )


def test_unzip_stream_max_archive_size_spooled(tmp_path, async_reader):
    archive = _make_archive(tmp_path / "a.zip", [("a.txt", b"x" * 50000)])
    archive_bytes = (tmp_path / "a.zip").stat().st_size
    spool_dir = tmp_path / "spool"
    with pytest.raises(LimitExceeded) as info:
        asyncio.run(
            unzipper.unzip_stream(
                _chunks_of(archive, step=1024),
                path=tmp_path / "out",
                spool_dir=spool_dir,
                max_archive_size=archive_bytes // 2,
            )
        )
    assert info.value.limit == "max_archive_size"
    # The partial spool file must be cleaned up.
    assert not spool_dir.exists() or not any(spool_dir.iterdir())


def test_unzip_stream_max_archive_size_in_memory(tmp_path, async_reader):
    archive = _make_archive(tmp_path / "a.zip", [("a.txt", b"x" * 50000)])
    archive_bytes = (tmp_path / "a.zip").stat().st_size
    with pytest.raises(LimitExceeded):
        asyncio.run(
            unzipper.unzip_stream(
                _chunks_of(archive, step=1024),
                path=tmp_path / "out",
                spool_dir=tmp_path / "spool",
                in_memory=True,
                max_archive_size=archive_bytes // 2,
            )
        )
