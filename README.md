# async-unzip
Asynchronous unzipping of big files with low memory usage in Python
Helps with big zip files unpacking (memory usage + buffer_size could be changed).
Also, prevents having Asyncio Timeout errors especially in case of many workers using same CPU cores.

Fully tested on Python 3.9 through 3.14.

Extraction is hardened against malicious archives: entry names that try to
escape the destination directory ("Zip Slip", via `../` or absolute paths)
are rejected, and encrypted entries raise a clear error instead of writing
garbage. Each entry is integrity-checked while it streams — extraction aborts
if a deflate stream is truncated, inflates past the uncompressed size declared
in its header, or fails its CRC-32 — so corrupt or size-/CRC-spoofed entries
are caught rather than written. (These are consistency checks against the
archive's own metadata; they do not cap an entry whose header honestly
declares a huge size — for that, set the resource limits below.) `unzip` and
`unzip_stream` return the list of paths written to disk.

For honestly-declared oversized archives, opt into resource limits
(`max_entries`, `max_entry_size`, `max_total_uncompressed_size`, and
`max_archive_size` for streams). They are checked up front and raise
`LimitExceeded` before extraction begins:

```python
from async_unzip import unzip, LimitExceeded

asyncio.run(unzip(
    "untrusted.zip",
    path="output",
    max_entries=10_000,
    max_entry_size=500 * 1024 * 1024,
    max_total_uncompressed_size=2 * 1024 * 1024 * 1024,
))
```

By default the extractor schedules up to 4 concurrent workers using the stdlib `zlib` backend. Tune concurrency via the `max_workers` argument, and install `python-isal` or `zlib-ng` if you want to force those accelerators via the optional `backend` parameter:

```python
asyncio.run(unzip('archive.zip', path='output', max_workers=8))
```

When `uvloop` is installed, the event loop policy switches automatically to leverage its faster reactor.

When `python-isal` or `zlib-ng` is installed you can opt into them via `backend="python-isal"` or `backend="zlib-ng"`; otherwise the stdlib `zlib` backend remains the default.

## Usage Examples

```python
import asyncio
from async_unzip import unzipper

# Basic usage (defaults to stdlib zlib, max_workers=4)
asyncio.run(unzipper.unzip("tests/test_files/fixture_beta.zip", path="output"))

# Force python-isal backend and custom worker count
asyncio.run(
    unzipper.unzip(
        "tests/test_files/fixture_gamma.zip",
        path="output_isal",
        backend="python-isal",
        max_workers=2,
    )
)

# Specify a whitelist of files and a regex filter
asyncio.run(
    unzipper.unzip(
        "archive.zip",
        path="filtered",
        files=["docs/readme.txt"],
        regex_files=[r"images/.*\\.png$"],
    )
)
```

#### `unzip` parameters

- `zip_file`: path-like reference to the ZIP archive.
- `path`: optional extraction root (defaults to current working directory).
- `files`: iterable of exact filenames to whitelist.
- `regex_files`: string or iterable of regex patterns used to include entries.
- `buffer_size`: override block size for reading each entry; defaults to auto.
- `max_workers`: maximum concurrent extraction coroutines (minimum 1).
- `backend`: decompressor choice (`zlib`, `python-isal`, `zlib-ng`).
- `max_entries` (keyword-only): maximum number of selected members; `None` = unlimited.
- `max_entry_size` (keyword-only): maximum uncompressed size of any single entry.
- `max_total_uncompressed_size` (keyword-only): maximum total uncompressed size.
- `__debug`: when truthy, prints internal seek/decompression events.

Returns a list of `pathlib.Path` objects for the entries written to disk.
Entries whose names escape `path` raise `BadZipFile`, encrypted entries raise
`NotImplementedError`, and a breached limit raises `LimitExceeded` (a distinct
exception, not a `BadZipFile`) before any file is written.

### Streaming downloads

Use `unzip_stream` when ZIP bytes arrive incrementally (for example, via
`aiohttp` or `httpx` downloads). The helper spools the incoming chunks to a
temporary file (optionally in a custom `spool_dir`) and then fans out the
extraction with the same backend/filter arguments as `unzip`.

#### `aiohttp` chunked download

```python
import aiohttp

async def download_and_extract(url, target_dir):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            await unzipper.unzip_stream(
                response.content.iter_chunked(64 * 1024),
                path=target_dir,
                backend="zlib-ng",
                spool_dir="/tmp/async-unzip",
            )
```

#### `httpx` chunked download (spooled with filters)

```python
import httpx

async def download_and_extract_httpx(url, target_dir):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            await unzipper.unzip_stream(
                response.aiter_bytes(chunk_size=128 * 1024),
                path=target_dir,
                files=["docs/readme.txt"],
                regex_files=[r"images/.*\\.png$"],
                backend="python-isal",
                max_workers=2,
                spool_dir="./zip-cache",
            )
```

#### `httpx` chunked download (in-memory single worker)

```python
import httpx

async def download_and_extract_httpx_mem(url, target_dir):
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            await unzipper.unzip_stream(
                response.aiter_bytes(chunk_size=64 * 1024),
                path=target_dir,
                backend="zlib",
                max_workers=1,
                buffer_size=32 * 1024,
                in_memory=True,
            )
```

#### `unzip_stream` parameters

- `chunk_iterable`: async iterable yielding bytes from the download stream.
- `path`: optional destination root mirroring `unzip`.
- `files`: whitelist list identical to `unzip`.
- `regex_files`: regex filters identical to `unzip`.
- `buffer_size`: read block override used during extraction.
- `max_workers`: concurrency cap reused when extracting from the spooled file.
- `backend`: decompressor name (`zlib`, `python-isal`, `zlib-ng`).
- `spool_dir`: optional directory for temporary storage (defaults to system temp).
- `in_memory`: **deprecated** — buffers the whole archive in RAM, decompresses synchronously, and ignores `backend`/`max_workers`. Use the default spooled path (with `spool_dir`) instead.
- `max_entries` / `max_entry_size` / `max_total_uncompressed_size` (keyword-only): same as `unzip`.
- `max_archive_size` (keyword-only): cap on raw bytes read from the stream; raises `LimitExceeded` before an oversized download is fully consumed.
- `__debug`: enables verbose progress logging just like `unzip`.

Returns the list of paths written to disk. Note that `in_memory=True`
decompresses each entry synchronously via the stdlib `zipfile` reader, so the
`backend` and `max_workers` options only affect the default (spooled) path.

### Optional backends

```bash
pip install python-isal    # to enable backend="python-isal"
pip install zlib-ng        # to enable backend="zlib-ng"
```

### Benchmark script

Use the helper under `scripts/bench_async_metrics.py` to reproduce the CPU/memory data in the tables below:

```bash
python scripts/bench_async_metrics.py \
  --archives tests/test_files/fixture_gamma.zip \
             large:~/Downloads/some_large.zip \
  --workers 1 2 4 \
  --backend zlib-ng \
  --samples 3
```

## Benchmarks

Numbers below were captured on an Apple Silicon macOS Sonoma machine (ARM64). Each measurement extracts into a fresh temporary directory and averages three runs.

### Synthetic archive (`tests/test_files/fixture_gamma.zip`, 23.7 MB)

| Backend      | Workers | Avg time (s) | CPU avg / max (%) | RAM avg / max (MB) |
|--------------|---------|--------------|-------------------|--------------------|
| zlib         | 1       | 0.91         | 85.7 / 89.3       | 29.46 / 29.52      |
| zlib         | 2       | 0.80         | 117.6 / 133.3     | 32.32 / 32.52      |
| zlib         | 4       | 0.70         | 162.3 / 167.4     | 33.24 / 33.34      |
| zlib-ng      | 1       | 1.00         | 81.7 / 87.7       | 29.44 / 29.59      |
| zlib-ng      | 2       | 0.80         | 119.4 / 133.7     | 32.62 / 32.86      |
| zlib-ng      | 4       | 0.82         | 134.4 / 168.3     | 33.59 / 33.70      |
| python-isal  | 1       | 1.12         | 76.8 / 92.6       | 29.71 / 29.84      |
| python-isal  | 2       | 0.91         | 112.4 / 132.0     | 33.01 / 33.11      |
| python-isal  | 4       | 0.80         | 146.1 / 163.1     | 34.08 / 34.19      |

### Real dataset (external ZIP, ≈1.10 GB)

| Backend      | Workers | Avg time (s) | CPU avg / max (%) | RAM avg / max (MB) |
|--------------|---------|--------------|-------------------|--------------------|
| zlib         | 1       | 9.49         | 81.2 / 98.4       | 75.21 / 79.25      |
| zlib         | 2       | 8.84         | 87.6 / 126.2      | 78.88 / 79.38      |
| zlib         | 4       | 8.56         | 90.2 / 128.1      | 84.87 / 84.94      |
| zlib-ng      | 1       | 13.35        | 73.0 / 96.7       | 37.95 / 38.95      |
| zlib-ng      | 2       | 13.15        | 84.1 / 120.1      | 205.45 / 243.17    |
| zlib-ng      | 4       | 12.12        | 92.4 / 121.7      | 218.62 / 244.89    |
| python-isal  | 1       | 20.00        | 95.8 / 100.0      | 37.58 / 38.33      |
| python-isal  | 2       | 21.76        | 96.2 / 110.5      | 202.98 / 244.09    |
| python-isal  | 4       | 22.00        | 96.2 / 112.5      | 217.48 / 246.03    |

The large archive is not part of this repository; download any similarly sized ZIP manually if you want to reproduce the numbers.

#### Synchronous `zipfile.ZipFile.extractall()` (same 1.10 GB dataset)

| Backend      | Avg seconds | Samples (s)                     |
|--------------|-------------|---------------------------------|
| zlib         | 14.42       | 14.58, 14.53, 14.16             |
| zlib-ng      | 14.94       | 14.98, 14.99, 14.83             |
| python-isal  | 14.04       | 13.92, 14.24, 13.94             |

`zipfile` is single-threaded, so concurrency does not apply in this scenario.

From version 0.3.6 module doesn't require, but expects to have `aiofile` OR `aiofiles` to be installed for I/O operations.
However, `aiofile` is recommended for linux, just don't forget to install `libaio` (`libaio1`) linux module (e.g., `apt install -y libaio1` for debian)

```python
from async_unzip.unzipper import unzip
import asyncio

asyncio.run(unzip('tests/test_files/fixture_beta.zip', path='some_dir'))
```

## Changelog

### 0.8.0
- uvloop is no longer enabled as an unconditional import-time side effect. It still auto-installs its event-loop policy by default, but only when the host application hasn't already set a policy and hasn't set the `ASYNC_UNZIP_NO_UVLOOP` environment variable.
- Debug output now goes through the `logging` module (`async_unzip.unzipper` logger at DEBUG level) instead of `print()`. The `__debug` parameter is renamed `debug`; `__debug` remains a deprecated keyword-only alias that emits a `DeprecationWarning`.
- Stopped mutating the `DECOMPRESS_BACKEND`/`LAST_USED_BACKEND` module globals on every call (concurrent calls raced); the names remain as constants.
- Packaging migrated to PEP 621 (`pyproject.toml`); `setup.py` removed. Added a `py.typed` marker and type hints on the public API, checked by `mypy` in CI. Added a coverage floor to CI.
- `unzip_stream(in_memory=True)` removal is now scheduled for 1.0.0.

### 0.7.0
- Add opt-in resource limits to `unzip`/`unzip_stream` (keyword-only, default unlimited): `max_entries`, `max_entry_size`, `max_total_uncompressed_size`, and `max_archive_size` (streams only). Breaches raise a new `LimitExceeded` exception — distinct from `BadZipFile` so policy refusals are distinguishable from corruption. Entry limits are enforced up front from the central directory (authoritative, because each entry is integrity-checked to produce exactly its declared size), so nothing is written before a breach is detected.
- Expose `unzip`, `unzip_stream`, and `LimitExceeded` at the top level: `from async_unzip import unzip`.
- Reject unsupported compression methods consistently: entries that are neither stored nor deflate (e.g. bzip2, lzma) now raise `NotImplementedError` on both the spooled and in-memory paths, instead of leaking an opaque zlib error (spooled) or silently decoding only in memory.
- Deprecate `unzip_stream(in_memory=True)` (emits `DeprecationWarning`): it buffers the whole archive in RAM, decompresses synchronously, and ignores `backend`/`max_workers`, which works against the library's low-memory/async goals. Use the default spooled path (with `spool_dir`) instead; it will be removed in a future release.

### 0.6.1
- Security: fix an infinite-loop denial of service in the deflate reader. A crafted archive declaring a `compress_size` larger than its real stream made decompression spin forever (and grow memory); the reader now stops at the deflate end-of-stream marker.
- Fix a guard hole where an entry declaring `file_size == 0` disabled the inflate check, letting a deflated entry write unbounded bytes.
- Add stdlib-parity integrity validation: every entry's CRC-32 is verified, the decompressed length must equal the declared size exactly, truncated streams are rejected, and stored entries must have matching declared sizes. `Z_SYNC_FLUSH`-terminated streams (which never set the deflate end-of-stream marker) are accepted when their size and CRC agree, matching `zipfile`.
- Extract each entry atomically: stream into a temporary file and move it into place only after every integrity check passes, so a failed CRC/size check never leaves a corrupt or partial file at the destination.
- Reword the "zip-bomb guard" description as the consistency check it actually is (see 0.6.0 note below).
- CI: install optional accelerators best-effort per package, but gate the accelerator test run so real `isal`/`zlib-ng`/`uvloop` regressions fail the build.

### 0.6.0
- Security: reject "Zip Slip" entries whose names escape the destination directory (relative `../` traversal and absolute paths).
- Reject encrypted entries with a clear `NotImplementedError` instead of writing corrupt output.
- Bound decompression output per block (caps peak memory on `zlib`/`zlib-ng`) and abort extraction if an entry inflates beyond its declared uncompressed size — a backend-independent consistency check against corrupt or size-spoofed entries. (Not a full zip-bomb defense: an entry whose header honestly declares a huge size is still extracted; configurable size/entry-count limits are planned.)
- `unzip` and `unzip_stream` now return the list of paths written to disk.
- Offload central-directory parsing to a worker thread and make the streaming spool write asynchronously, so the event loop is no longer blocked on those I/O paths.
- Bound the process-global window-bits cache so long-running services do not grow it without limit.
- Replace the missing-backend `print` with a logging warning.
- Packaging: require Python 3.9+ (uses `asyncio.to_thread`), refresh classifiers, add `isal`/`zlib-ng`/`speedups` extras, and add a GitHub Actions CI matrix (3.9–3.14) plus lint.

### 0.5.5
- Fixed per-entry window-bits caching to avoid incorrect header errors on mixed compression modes.
- Documented new streaming parameter details and added the consolidated `run_tests.sh` helper.

### 0.5.4
- Added `aiohttp` and `httpx` chunked-download examples for `unzip_stream`.
- Bumped version metadata to 0.5.4 to reflect the new streaming API and docs.

### 0.5.2
- Added usage examples, backend-installation notes, and benchmark-script instructions.
- Introduced zlib backend selection parameter (`backend="..."`) and made stdlib zlib the explicit default even when accelerators are installed.
- Added deterministic benchmark script under `scripts/bench_async_metrics.py` for reproducibility.

### 0.5.1
- Added zlib-ng backend option alongside python-isal and documented how to select backends explicitly.
- Introduced backend registry and optional `backend="..."` parameter (defaulting to stdlib zlib).
- Documented detailed time/CPU/memory benchmarks for async extraction on Apple Silicon macOS.
- Added synchronous `zipfile.extractall()` comparison table.

### 0.5.0
- Added directory-creation caching and adaptive buffer sizing.
- Introduced backend auto-detection for python-isal; updated tests to cover new behaviors.
- Improved README benchmark section.

### 0.4.x
- Initial async unzipper with concurrency, window-bits caching, and various bug fixes.

# test
```bash
pip install tox
tox
```
