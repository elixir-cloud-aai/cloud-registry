"""Package setup."""

from pathlib import Path
from setuptools import (setup, find_packages)

root_dir = Path(__file__).parent.resolve()

exec(open(root_dir / "cloud_registry" / "version.py").read())

file_name = root_dir / "README.md"
with open(file_name, "r") as _file:
    long_description = _file.read()

setup(
    name='cloud-registry',
    version=__version__,  # noqa: F821
    author='ELIXIR Cloud & AAI',
    author_email='alexander.kanitz@alumni.ethz.ch',
    description=(
        'GA4GH Service Registry API implementation for the ELIXIR Cloud'
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    license='Apache License 2.0',
    url='https://github.com/elixir-cloud-aai/cloud-registry.git',
    packages=find_packages(),
    keywords=(
        'ga4gh service registry elixir cloud rest restful api app server '
        'openapi swagger mongodb python flask'
    ),
    classifiers=[
        'License :: OSI Approved :: Apache Software License',
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Science/Research',
        'Topic :: Scientific/Engineering :: Bio-Informatics',
        'Natural Language :: English',
        'Programming Language :: Python :: 3.8',
    ],
    install_requires=[],
)
