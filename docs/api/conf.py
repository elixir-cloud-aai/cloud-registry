# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
from pathlib import Path
import sys

from setuptools_scm import get_version
from sphinx.ext import apidoc

docs_dir = Path(__file__).resolve().parent
root_dir = docs_dir.parents[1]

# Document the checked-out sources; their dependencies must be installed
# (Read the Docs installs the package, cf. `.readthedocs.yaml`), otherwise
# autodoc cannot import the modules and the module pages stay empty
sys.path.insert(0, str(root_dir))

# The version comes from Git tags (cf. `[tool.setuptools_scm]` in
# `pyproject.toml`)
__version__ = get_version(root=str(root_dir), fallback_version="0.0.0.dev0")


# -- Project information -----------------------------------------------------

project = "ELIXIR Cloud service registry"
copyright = "2020, ELIXIR Cloud & AAI"
author = "ELIXIR Cloud & AAI"

# The full version, including alpha/beta/rc tags
release = __version__  # noqa: F821


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
]

# Render `Attributes` sections of docstrings as fields of their class, as
# autodoc documents the attributes of models already (otherwise duplicates)
napoleon_use_ivar = True

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build"]

# Default doc to search for
master_doc = "index"

# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = "sphinx_rtd_theme"

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = []


# -- Automation -------------------------------------------------------------


# Auto-generate API doc (sources in `modules/`, ignored by Git)
def run_apidoc(_):
    argv = [
        "--force",
        "--module-first",
        # `cloud_registry.service_models` has no `__init__.py`
        "--implicit-namespaces",
        "-o",
        str(docs_dir / "modules"),
        str(root_dir / "cloud_registry"),
    ]
    apidoc.main(argv)


def setup(app):
    app.connect("builder-inited", run_apidoc)
