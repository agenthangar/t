"""Resolve existing repository selectors without changing registrations."""
import os


def resolve_repo(repos, selector, *, required=False):
    """Return a registered alias, or None for an unknown optional selector.

    Exact aliases keep their own policy. Directory names may identify only one
    real checkout; aliases of that checkout use a stable shortest/lexical choice.
    Ambiguity is always an error, including in mixed repository/session grammars.
    """
    if selector in repos:
        return selector
    matches = [alias for alias, path in repos.items()
               if selector and os.path.basename(os.path.normpath(path)) == selector]
    if len({os.path.realpath(repos[alias]) for alias in matches}) > 1:
        raise ValueError(f"ambiguous repository {selector!r}; use an explicit alias: "
                         + ", ".join(sorted(matches)))
    if matches:
        return min(matches, key=lambda alias: (len(alias), alias))
    if required:
        raise ValueError(f"unknown repository {selector!r}; registered aliases: "
                         + (", ".join(sorted(repos)) or "(none registered)"))
    return None
