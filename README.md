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

From version 0.3.6 module doesn't require, but expects to have `aiofile` OR `aiofiles` to be installed for I/O operations.
However, `aiofile` is recommended for linux, just don't forget to install `libaio` (`libaio1`) linux module (e.g., `apt install -y libaio1` for debian)

```python
from async_unzip.unzipper import unzip
import asyncio

asyncio.run(unzip('tests/test_files/fixture_beta.zip', path='some_dir'))
```
