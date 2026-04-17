"""
Platform adapters for messaging integrations.

Keep package imports resilient across offline bundles that may be built from a
base release with a slightly different platform set.
"""

from .base import BasePlatformAdapter, MessageEvent, SendResult

__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
]

try:
    from .aops import AopsAdapter
except ImportError:
    AopsAdapter = None
else:
    __all__.append("AopsAdapter")

try:
    from .qqbot import QQAdapter
except ImportError:
    QQAdapter = None
else:
    __all__.append("QQAdapter")
