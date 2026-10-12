"""Construct per-conversation models using the enterprise gateway only."""

import logging
from typing import Optional

from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel

from app.config import settings

logger = logging.getLogger(__name__)
_model_cache: dict = {}
_credential: Optional[OpenAICredential] = None


def _get_credential() -> OpenAICredential:
    global _credential
    if _credential is None:
        if not settings.AI_GATEWAY_API_KEY:
            raise ValueError("AI_GATEWAY_API_KEY not configured")
        _credential = OpenAICredential(
            api_key=settings.AI_GATEWAY_API_KEY,
            base_url=settings.AI_GATEWAY_BASE_URL,
        )
    return _credential


def provider_for_model(model_name: str) -> str:
    return "enterprise-gateway"


def _parameters(parameters):
    if parameters is None:
        return None
    return OpenAIChatModel.Parameters.model_validate(parameters.model_dump())


def get_model(model_name, parameters=None, context_size=131072, fresh: bool = False):
    converted = _parameters(parameters)
    params_key = converted.model_dump_json() if converted is not None else ""
    cache_key = f"{model_name}|{params_key}|{context_size}"
    if fresh or cache_key not in _model_cache:
        model = OpenAIChatModel(
            credential=_get_credential(), model=model_name, parameters=converted,
            stream=True, context_size=context_size,
        )
        if fresh:
            return model
        _model_cache[cache_key] = model
        logger.info("[AgentScope] model init: model=%s provider=enterprise-gateway", model_name)
    return _model_cache[cache_key]
