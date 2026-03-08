import logging
import os

from cio.clients.llm_client import CIO_LLM_Client, LiteLLMClient, MockLLMClient

logger = logging.getLogger(__name__)


class ClientFactory:
    """
    Centralized factory for creating and managing LLM clients.
    Primary owner of the LLM_PROVIDER configuration logic.
    """

    _instance: CIO_LLM_Client | None = None

    @classmethod
    def reset(cls) -> None:
        """Reset singleton for testing. Never call in production code."""
        cls._instance = None

    @classmethod
    def create(cls, force_recreate: bool = False) -> CIO_LLM_Client:
        """
        Create (or retrieve) the configured LLM client.

        Args:
            force_recreate: If True, ignores existing instance and creates new.

        Returns:
            A concrete implementation of CIO_LLM_Client.
        """
        if cls._instance is not None and not force_recreate:
            return cls._instance

        provider = os.getenv("LLM_PROVIDER", "mock").lower()

        if provider == "mock":
            logger.info("Initializing MockLLMClient for development/testing.")
            cls._instance = MockLLMClient()
        elif provider in ("gemini", "openai", "litellm"):
            logger.info(f"Initializing LiteLLMClient with provider: {provider}")
            cls._instance = LiteLLMClient()
        else:
            if os.getenv("ENVIRONMENT", "development") == "production":
                raise ValueError(
                    f"Unknown LLM_PROVIDER '{provider}' in production environment"
                )

            logger.error(
                f"Unknown LLM_PROVIDER '{provider}'. Defaulting to mock — NOT safe for production."
            )
            cls._instance = MockLLMClient()

        return cls._instance
