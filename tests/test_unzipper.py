"""Tests for async_unzip.unzipper."""

# pylint: disable=protected-access,too-many-arguments,too-few-public-methods
# pylint: disable=too-many-locals,missing-function-docstring

import asyncio
import builtins
import gzip
import importlib
import sys
import types
from typing import Set
import zlib
from pathlib import Path
import zipfile

from zipfile import BadZipFile
from zlib import error as ZLIB_error

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from async_unzip import unzipper


FIXTURES_DIR = Path(__file__).parent / "test_files"
FIXTURE_EXPECTATIONS = {
    "fixture_alpha.zip": {
        "reader": "aiofiles",
        "use_cwd": False,
        "buffer_size": None,
        "use_debug": False,
        "files": ["benefits-and-cost-sharing-puf.csv"],
    },
    "fixture_beta.zip": {
        "reader": "aiofile",
        "use_cwd": True,
        "buffer_size": None,
        "use_debug": False,
    },
    "fixture_gamma.zip": {
        "reader": "aiofiles",
        "use_cwd": False,
        "buffer_size": None,
        "use_debug": False,
        "files": [
            "async-unzip/LICENSE",
            "async-unzip/README.md",
            "async-unzip/tests/test_files/small.zip",
        ],
    },
    "fixture_delta.zip": {
        "reader": "aiofiles",
        "use_cwd": False,
        "buffer_size": 256,
        "use_debug": True,
    },
    "fixture_epsilon.zip": {
        "reader": "aiofiles",
        "use_cwd": False,
        "buffer_size": None,
        "use_debug": True,
    },
    "fixture_zeta.zip": {
        "reader": "aiofile",
        "use_cwd": False,
        "buffer_size": None,
        "use_debug": False,
        "files": [
            (
                "xACT2.44/xACT.app/Contents/Resources/"
                "English.lproj/InfoPlist.strings"
            )
        ],
    },
    "fixture_eta.zip": {
        "reader": "aiofile",
        "use_cwd": False,
        "buffer_size": None,
        "use_debug": False,
    },
}


def _make_async_open(sync_seek: bool):
    """Create an aiofile/aiofiles-compatible factory backed by stdlib I/O."""

    class _AsyncFileHandle:
        def __init__(self, file_obj):
            self._fp = file_obj

        async def read(self, size=-1):
            return self._fp.read(size)

        async def write(self, data):
            written = self._fp.write(data)
            self._fp.flush()
            return written

        if sync_seek:

            def seek(self, offset, whence=0):
                self._fp.seek(offset, whence)
                return self._fp.tell()

        else:

            async def seek(self, offset, whence=0):
                self._fp.seek(offset, whence)
                return self._fp.tell()

    class _AsyncContextManager:
        def __init__(self, path, mode):
            self._path = Path(path)
            self._mode = mode
            self._fp = None

        async def __aenter__(self):
            encoding = None if "b" in self._mode else "utf-8"
            self._fp = open(  # pylint: disable=consider-using-with
                self._path,
                self._mode,
                encoding=encoding,
            )
            return _AsyncFileHandle(self._fp)

        async def __aexit__(self, exc_type, exc, tb):
            if self._fp:
                self._fp.close()

    def _async_open(path, mode="rb"):
        return _AsyncContextManager(path, mode)

    return _async_open


def _configure_async_reader(monkeypatch, reader: str):
    sync_seek = reader == "aiofile"
    monkeypatch.setattr(
        unzipper,
        "async_open",
        _make_async_open(sync_seek),
        raising=False,
    )
    monkeypatch.setattr(unzipper, "async_reader", reader)


def _expected_files(zip_path: Path, selected=None):
    selected_lookup: Set[str] = set(selected) if selected else set()
    use_subset = bool(selected_lookup)

    def include(entry):
        if entry.is_dir():
            return False
        if not use_subset:
            return True
        return entry.filename in selected_lookup

    with zipfile.ZipFile(zip_path) as archive:
        return {
            Path(info.filename).as_posix(): info.file_size
            for info in archive.infolist()
            if include(info)
        }


def _extracted_files(root: Path):
    return {
        path.relative_to(root).as_posix(): path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
    }


def test_fixture_manifest_matches_directory():
    disk = {path.name for path in FIXTURES_DIR.glob("*.zip")}
    assert disk == set(FIXTURE_EXPECTATIONS)


@pytest.mark.parametrize(
    ("zip_name", "reader", "use_cwd", "buffer_size", "use_debug"),
    [
        (
            name,
            spec["reader"],
            spec["use_cwd"],
            spec["buffer_size"],
            spec["use_debug"],
        )
        for name, spec in FIXTURE_EXPECTATIONS.items()
    ],
)
# pylint: disable-next=too-many-positional-arguments
def test_unzipper_extracts_fixture_archives(
    zip_name,
    reader,
    use_cwd,
    buffer_size,
    use_debug,
    tmp_path,
    monkeypatch,
    capsys,
):
    _configure_async_reader(monkeypatch, reader)
    destination = tmp_path if use_cwd else tmp_path / Path(zip_name).stem
    if not use_cwd:
        destination.mkdir(parents=True, exist_ok=True)
    path_arg = None if use_cwd else destination
    if use_cwd:
        monkeypatch.chdir(destination)

    spec = FIXTURE_EXPECTATIONS[zip_name]
    kwargs = {"path": path_arg}
    if buffer_size is not None:
        kwargs["buffer_size"] = buffer_size
    if use_debug:
        kwargs["__debug"] = True
    if spec.get("files"):
        kwargs["files"] = spec["files"]
    if spec.get("regex"):
        kwargs["regex_files"] = spec["regex"]
    archive_path = FIXTURES_DIR / zip_name
    asyncio.run(unzipper.unzip(str(archive_path), **kwargs))

    expected = _expected_files(archive_path, selected=spec.get("files"))
    actual_root = destination
    actual = _extracted_files(actual_root)
    assert actual == expected

    if use_debug:
        debug_output = capsys.readouterr().out
        assert "Done HEADER_OFFSET seek" in debug_output


def test_unzipper_handles_entry_comments(tmp_path, monkeypatch):
    info = zipfile.ZipInfo("annotated/data.txt")
    info.comment = b"unit-test"
    archive_path = tmp_path / "commented.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, b"payload")

    _configure_async_reader(monkeypatch, "aiofiles")
    destination = tmp_path / "commented"
    asyncio.run(
        unzipper.unzip(str(archive_path), path=destination, __debug=True)
    )
    assert (destination / "annotated" / "data.txt").read_text() == "payload"


def test_unzipper_imports_without_async_backends(monkeypatch):
    original_import = builtins.__import__

    def fake_import(
        name,
        globals_map=None,
        locals_map=None,
        fromlist=(),
        level=0,
    ):
        if name in {"aiofile", "aiofiles"}:
            raise ModuleNotFoundError(name)
        return original_import(name, globals_map, locals_map, fromlist, level)

    with monkeypatch.context() as ctx:
        ctx.setattr(builtins, "__import__", fake_import)
        module = importlib.reload(unzipper)
        assert module.missed_modules == 2
    importlib.reload(unzipper)


def test_unzipper_prefers_aiofiles_when_available(monkeypatch):
    dummy = types.ModuleType("aiofiles")

    def fake_open(*_args, **_kwargs):
        return object()

    dummy.open = fake_open

    with monkeypatch.context() as ctx:
        ctx.setitem(sys.modules, "aiofiles", dummy)
        module = importlib.reload(unzipper)
        assert module.async_reader == "aiofiles"
    importlib.reload(unzipper)


def test_unzipper_rejects_non_zip_payload(tmp_path):
    bogus = tmp_path / "not-a-zip.txt"
    bogus.write_text("plain text")
    with pytest.raises(BadZipFile):
        asyncio.run(unzipper.unzip(str(bogus)))


def test_unzipper_requires_async_backend(monkeypatch, tmp_path):
    archive_path = FIXTURES_DIR / "fixture_delta.zip"
    monkeypatch.setattr(unzipper, "async_open", None, raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(unzipper.unzip(str(archive_path), path=tmp_path))


def test_files_argument_limits_extraction(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch, "aiofiles")
    archive_path = FIXTURES_DIR / "fixture_beta.zip"
    target = tmp_path / "subset"
    subset = ["nvidiame/NVAGP.INF"]
    asyncio.run(unzipper.unzip(str(archive_path), path=target, files=subset))
    extracted = _extracted_files(target)
    assert set(extracted) == set(subset)


def test_regex_argument_limits_extraction(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch, "aiofiles")
    archive_path = FIXTURES_DIR / "fixture_delta.zip"
    target = tmp_path / "regex"
    pattern = r"2018\.1"
    asyncio.run(
        unzipper.unzip(
            str(archive_path),
            path=target,
            regex_files=[pattern],
        )
    )
    extracted = _extracted_files(target)
    expected_name = "7F3Y8GSKKH - for 2018.1 or earlier.txt"
    assert set(extracted) == {expected_name}


def test_regex_argument_accepts_string(tmp_path, monkeypatch):
    _configure_async_reader(monkeypatch, "aiofiles")
    archive_path = FIXTURES_DIR / "fixture_delta.zip"
    target = tmp_path / "regex-string"
    pattern = r"2018\.2"
    asyncio.run(
        unzipper.unzip(
            str(archive_path),
            path=target,
            regex_files=pattern,
        )
    )
    extracted = _extracted_files(target)
    expected_name = "7F3Y8GSKKH - for 2018.2 or later.txt"
    assert set(extracted) == {expected_name}


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size=-1):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _AsyncRecorder:
    def __init__(self):
        self.data = []

    async def write(self, payload):
        self.data.append(payload)


def test_read_local_header_rejects_short_headers():
    class _ShortStream:
        async def read(self, _size=-1):
            return b"PK"

    with pytest.raises(BadZipFile):
        asyncio.run(unzipper._read_local_header(_ShortStream(), "file.txt"))


def test_write_stored_entry_detects_truncation():
    stream = _AsyncChunkStream([b"", b""])

    class _Sink:
        async def write(self, _):
            raise AssertionError("should not write")

    with pytest.raises(BadZipFile):
        asyncio.run(
            unzipper._write_stored_entry(
                stream,
                _Sink(),
                remaining=10,
                read_block=4,
                file_name="stored",
            )
        )


def test_write_compressed_entry_handles_empty_payload():
    recorder = _AsyncRecorder()
    asyncio.run(
        unzipper._write_compressed_entry(
            _AsyncChunkStream([]),
            recorder,
            remaining=0,
            read_block=4,
            file_name="empty",
        )
    )
    assert recorder.data == [b""]


def test_write_compressed_entry_rejects_empty_initial_chunk():
    stream = _AsyncChunkStream([b""])
    with pytest.raises(BadZipFile):
        asyncio.run(
            unzipper._write_compressed_entry(
                stream,
                _AsyncRecorder(),
                remaining=5,
                read_block=4,
                file_name="compressed",
            )
        )


def test_write_compressed_entry_fails_when_window_not_detected(monkeypatch):
    stream = _AsyncChunkStream([b"\x00\x00\x00\x00"])

    class _BrokenDecomp:
        def decompress(self, _):
            raise ZLIB_error("boom")

        def flush(self):
            return b""

    monkeypatch.setattr(unzipper, "decompressobj", lambda _: _BrokenDecomp())

    with pytest.raises(ZLIB_error):
        asyncio.run(
            unzipper._write_compressed_entry(
                stream,
                _AsyncRecorder(),
                remaining=4,
                read_block=4,
                file_name="bad",
            )
        )


def test_write_compressed_entry_detects_truncated_stream():
    payload = zlib.compress(b"payload")
    stream = _AsyncChunkStream([payload, b""])
    with pytest.raises(BadZipFile):
        asyncio.run(
            unzipper._write_compressed_entry(
                stream,
                _AsyncRecorder(),
                remaining=len(payload) + 5,
                read_block=len(payload),
                file_name="payload",
            )
        )


def test_write_compressed_entry_logs_failed_window_bits(capsys):
    payload = gzip.compress(b"payload")
    stream = _AsyncChunkStream([payload])
    recorder = _AsyncRecorder()
    asyncio.run(
        unzipper._write_compressed_entry(
            stream,
            recorder,
            remaining=len(payload),
            read_block=len(payload),
            file_name="gzip",
            __debug=True,
        )
    )
    assert b"payload" in b"".join(recorder.data)
    assert "Failed WindowBits" in capsys.readouterr().out
