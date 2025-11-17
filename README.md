# async-unzip
Asynchronous unzipping of big files with low memory usage in Python
Helps with big zip files unpacking (memory usage + buffer_size could be changed).
Also, prevents having Asyncio Timeout errors especially in case of many workers using same CPU cores.

Fully tested on Python 3.7 through 3.14.

By default the extractor schedules up to 4 concurrent workers. Tune concurrency via the `max_workers` argument:

```python
asyncio.run(unzip('archive.zip', path='output', max_workers=8))
```

When `uvloop` is installed, the event loop policy switches automatically to leverage its faster reactor.

When `python-isal` or `zlib-ng` is installed, async-unzip automatically switches to their faster zlib-compatible decompressors; otherwise it falls back to the standard library zlib.

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
