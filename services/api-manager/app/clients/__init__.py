"""Clients for external services: Vault, SPIRE, etc."""

from app.clients.spire import SpireClient
from app.clients.vault import VaultClient

__all__ = ["VaultClient", "SpireClient"]
