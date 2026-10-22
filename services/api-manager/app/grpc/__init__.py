"""Package holding protoc-generated gRPC stubs (``app.grpc.gough``).

The generated ``*_pb2_grpc.py`` modules use bare ``from gough import ...``.
Putting this directory on ``sys.path`` at package-import time makes that
resolve for every importer, not only ``app.grpc_runner``/``app.grpc_server``
(previously an HTTP-only process with GRPC_ENABLED=false hit
``ModuleNotFoundError: No module named 'gough'`` on the primary endpoint path).
"""

import os
import sys

_GRPC_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _GRPC_PKG_DIR not in sys.path:
    sys.path.insert(0, _GRPC_PKG_DIR)
