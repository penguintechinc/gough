"""Clients for external services: Vault, SPIRE, etc."""

from app.clients.vault import VaultClient
from app.clients.spire import SpireClient

__all__ = ["VaultClient", "SpireClient"]
