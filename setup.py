# -*- coding: utf-8 -*-
import re
from setuptools import setup

REQUIRES = [
]

def find_version(fname):
    """Attempts to find the version number in the file names fname.
    Raises RuntimeError if not found.
    """
    version = ""
    with open(fname, "r") as fp:
        reg = re.compile(r'__version__ = [\'"]([^\'"]*)[\'"]')
        for line in fp:
            m = reg.match(line)
            if m:
                version = m.group(1)
                break
    if not version:
        raise RuntimeError("Cannot find version information")
    return version

__version__ = find_version("async_unzip/__init__.py")

missed_modules = 0

try:
    from aiofile import async_open
except ModuleNotFoundError as err:
    missed_modules += 1

try:
    from aiofiles import open as async_open
except ModuleNotFoundError as err:
    missed_modules += 1
except ImportError as err:
    missed_modules += 1

if missed_modules == 2:
    print("""Not aiofile nor aiofiles is present! Going to crash..
        please do:
            pip install aiofile
        or
            pip install aiofiles
        to make the code working, Thanks!""")

setup(
    name="async-unzip",
    version=__version__,
    description="Async unzipping to prevent asyncio timeout errors and decrease the memory usage for bigger zip files",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Dmytro Nikolayev",
    author_email="dnikolayev@gmail.com",
    url="https://github.com/ueni-ltd/async-unzip",
    packages=["async_unzip"],
    install_requires=REQUIRES,
    license="MIT",
    zip_safe=False,
    keywords="async unzip",
    classifiers=[
        "Intended Audience :: Developers",
        "Environment :: Web Environment",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    requirements = REQUIRES,
    tests_require = ["pytest", "pytest-cov"],
)