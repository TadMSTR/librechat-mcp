# The single source of truth for this package's version.
#
# pyproject.toml declares `dynamic = ["version"]` and reads this attribute at build
# time, and client.py interpolates it into the User-Agent. Before v0.2.0 the version
# was written out in all three places independently: __init__ and pyproject said
# 0.1.0, the UA hardcoded 0.1.0, and the CHANGELOG had already released 0.1.1.
__version__ = "0.2.0"
