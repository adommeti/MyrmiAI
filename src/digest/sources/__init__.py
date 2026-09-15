"""Source adapters. Each one turns an external feed into ``ContentItem``s."""

from .base import SourceAdapter
from .web import WebLinkAdapter
from .youtube import YouTubeAdapter

__all__ = ["SourceAdapter", "WebLinkAdapter", "YouTubeAdapter"]
