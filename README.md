# async-unzip
Asynchronous unzipping of big files with low memory usage in Python
Helps with big zip files unpacking (memory usage + buffer_size could be changed).
Also, prevents having Asyncio Timeout errors especially in case of many workers using same CPU cores.

```python
from async_unzip.unzipper import unzip
import asyncio

asyncio.run(unzip('tests/test_files/nvidia_me.zip', path='some_dir'))
```
