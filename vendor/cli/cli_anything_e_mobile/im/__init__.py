"""cli_anything_e_mobile.im — E-Mobile 消息通道(基于 XMPP)。"""

from .xmpp import (NS_DING, NS_GROUP, NS_HISTORY, NS_SEARCH_HISTORY, ScramSha1,
                   XMPPClient, XMPPError, infer_target_type, resolve_proxy)

__all__ = ["XMPPClient", "XMPPError", "ScramSha1", "resolve_proxy",
           "infer_target_type", "NS_HISTORY", "NS_SEARCH_HISTORY",
           "NS_GROUP", "NS_DING"]