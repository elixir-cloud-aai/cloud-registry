from setuptools import (setup, find_packages)

from cloud_registry import __version__

with open('README.md', 'r') as fh:
    long_description = fh.read()

setup(
    name='cloud-registry',
    version=__version__,
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
