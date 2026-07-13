#!/usr/bin/env python3
"""Compatibility facade for Doctor network hardening.

The established implementation remains in ``doctor_base``. This module makes
DocuSeal probes ignore environment proxy settings so validated/pinned DNS is
actually used by the outbound connection.
"""
from __future__ import annotations

import sys
import urllib.request

import doctor_base as _impl


def _docuseal_opener() -> urllib.request.OpenerDirector:
    """Build a redirect-free, proxy-free opener for security-sensitive probes."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _impl._NoDocuSealRedirectHandler(),
    )


_impl._docuseal_opener = _docuseal_opener

if __name__ == "__main__":
    sys.modules.setdefault("doctor", _impl)
    raise SystemExit(_impl._main())

sys.modules[__name__] = _impl
