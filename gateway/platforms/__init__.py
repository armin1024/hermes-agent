"""
Platform adapters for messaging integrations.

Each adapter handles:
- Receiving messages from a platform
- Sending messages/responses back
- Platform-specific authentication
- Message formatting and media handling
"""

from .base import BasePlatformAdapter, MessageEvent, SendResult
from .qqbot import QQAdapter
from .yuanbao import YuanbaoAdapter

try:
    from .aops import AopsAdapter
except ImportError:
    AopsAdapter = None

__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
    "QQAdapter",
    "YuanbaoAdapter",
]
if AopsAdapter is not None:
    __all__.append("AopsAdapter")
