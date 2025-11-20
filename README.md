# async-unzip
Asynchronous unzipping of big files with low memory usage in Python
Helps with big zip files unpacking (memory usage + buffer_size could be changed).
Also, prevents having Asyncio Timeout errors especially in case of many workers using same CPU cores.

Fully tested on Python 3.7 through 3.14.

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
- `__debug`: when truthy, prints internal seek/decompression events.

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
- `in_memory`: when True, downloads into RAM and extracts without touching disk.
- `__debug`: enables verbose progress logging just like `unzip`.

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
