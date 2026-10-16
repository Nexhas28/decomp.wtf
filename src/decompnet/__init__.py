"""DecompNet: collaborative matching decompilation for AI agents."""

import sys

__version__ = "0.10.6"


if sys.platform == "darwin":
    # The Python builds uv installs on a Mac don't use the system's certificates at all: downloads fail with
    # CERTIFICATE_VERIFY_FAILED. Verify through the system itself instead, like every other program there does.
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
