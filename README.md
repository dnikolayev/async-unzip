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

### Synthetic archive (`tests/test_files/fixture_gamma.zip`, 23.7 MB, `max_workers=4`)

| Backend      | Avg seconds | Samples (s)                |
|--------------|-------------|----------------------------|
| zlib         | 0.024       | 0.0265, 0.0242, 0.0218     |
| zlib-ng      | 0.027       | 0.0282, 0.0265, 0.0277     |
| python-isal  | 0.061       | 0.0653, 0.0642, 0.0522     |

### Real dataset (external ZIP, ≈1.10 GB)

| Backend      | Max workers | Avg seconds | Samples (s)                         |
|--------------|-------------|-------------|-------------------------------------|
| zlib         | 1           | 8.59        | 8.63, 8.67, 8.49                    |
| zlib         | 2           | 8.44        | 8.49, 8.31, 8.51                    |
| zlib         | 4           | 8.42        | 8.32, 8.54, 8.40                    |
| zlib-ng      | 1           | 10.63       | 10.50, 11.24, 10.14                 |
| zlib-ng      | 2           | 11.66       | 12.25, 11.34, 11.38                 |
| zlib-ng      | 4           | 10.91       | 10.57, 11.65, 10.52                 |
| python-isal  | 1           | 20.38       | 20.43, 20.33, 20.39                 |
| python-isal  | 2           | 21.41       | 22.06, 20.36, 21.83                 |
| python-isal  | 4           | 21.26       | 22.03, 20.85, 20.89                 |

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
