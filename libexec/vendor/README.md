# Bundled Markdown parser

`mistune/` contains Mistune 3.3.4's parser, HTML renderer, and the plugins used by
`t session open-app` plan previews. Upstream: https://github.com/lepture/mistune. Its BSD 3-Clause
license is retained in `mistune/LICENSE`.

Source: the PyPI `mistune-3.3.4-py3-none-any.whl` release, SHA-256
`ee015381e955e370962968befe1d729ab60fafb6a715ac6751763fbce38c8d4a`.
Unused renderers, directives, plugins, and CLI entry points are omitted.

Two local compatibility changes:

- `core.py` uses `Any` for the annotation-only `Self` alias on Python <3.11,
  avoiding an otherwise unnecessary runtime dependency on typing-extensions.
- `plugins/__init__.py` resolves bundled plugins relative to the private package
  name so an unrelated system installation cannot replace the bundled parser.

The preview enables HTML escaping and keeps the renderer's safe-link filtering.
No package download, browser script, or external service is required at runtime.

# Bundled TOML parser

Tomli 2.2.1 (Python >=3.8), vendored without modification for Python 3.9 hosts.
Source: https://pypi.org/project/tomli/2.2.1/
Source archive SHA256: cd45e1dc79c835ce60f7404ec8119f2eb06d38b1deba146f07ced3bbc44505ff
License: tomli-LICENSE (MIT). No global Python installation is modified.
