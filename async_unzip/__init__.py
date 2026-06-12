"""Version definition to track changes"""

# Keep __version__ a plain string literal above the imports below: packaging
# (pyproject.toml [tool.setuptools.dynamic]) reads it statically at build time,
# and moving it below the import would force a build-time package import.
__version__ = "0.8.0"

from async_unzip.unzipper import LimitExceeded, unzip, unzip_stream

__all__ = ["LimitExceeded", "unzip", "unzip_stream", "__version__"]
