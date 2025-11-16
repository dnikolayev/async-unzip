"""Package configuration for async-unzip."""

import re
from pathlib import Path

from setuptools import setup

REQUIRES = []


def read_file(path):
    """Return the textual contents of a file."""
    return Path(path).read_text(encoding="utf-8")


def find_version(fname):
    """Extract __version__ from the target file."""
    version_regex = re.compile(r'__version__ = ["\']([^"\']*)["\']')
    for line in read_file(fname).splitlines():
        match = version_regex.match(line)
        if match:
            return match.group(1)
    raise RuntimeError("Cannot find version information")


setup(
    name="async-unzip",
    version=find_version("async_unzip/__init__.py"),
    description=(
        "Async unzipping to prevent asyncio timeout errors and decrease "
        "the memory usage for bigger zip files"
    ),
    long_description=read_file("README.md"),
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
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    tests_require=["pytest", "pytest-cov"],
)
