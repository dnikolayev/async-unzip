# async-unzip
Asynchronous unzipping of big files with low memory usage in Python
Helps with big zip files unpacking (memory usage + buffer_size could be changed).
Also, prevents having Asyncio Timeout errors especially in case of many workers using same CPU cores.

From version 0.3.6 module doesn't require, but expects to have `aiofile` OR `aiofiles` to be installed for I/O operations.
However, `aiofile` is recommended for linux, just don't forget to install `libaio` (`libaio1`) linux module (e.g., `apt install -y libaio1` for debian)

```python
from async_unzip.unzipper import unzip
import asyncio

asyncio.run(unzip('tests/test_files/nvidia_me.zip', path='some_dir'))
```
