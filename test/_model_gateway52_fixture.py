from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os

from app.config import settings
from app.database import SessionLocal, init_db


MODEL_IDS = ['ali-glm-5.2', 'ali-glm-5.3', 'ali-kimi/kimi-k3', 'ali-qwen-max', 'ali-qwen-turbo',
             'ali-qwen3.7-plus', 'ali-qwen3.8-flash', 'ali-qwen3.8-max', 'ali-qwen3.8-max-0902',
             'e-OvisOCR', 'e-bge-m3', 'e-deepseek-v4.1-flash', 'e-fun-asr-nano', 'e-glm-5.3',
             'e-qwen3-embedding-8b', 'e-qwen3.8-27b']
DISABLED = {'ali-qwen-max', 'ali-qwen-turbo', 'e-OvisOCR', 'e-bge-m3', 'e-fun-asr-nano', 'e-qwen3-embedding-8b'}


def create_app():
    if os.environ.get('MODEL_GATEWAY_ISOLATED') != '1' or not settings.DATABASE_URL.startswith('sqlite:'):
        raise RuntimeError('Fixture requires isolated SQLite and explicit isolation flag')
    from app.main import app
    from app.models.agent import Agent
    from app.models.model_governance import GatewayModelEntry, ModelMetadata
    from app.services import model_gateway, model_access, model_governance, agent_versions
    from app.api.v1 import models
    from _prepare_model_gateway52 import main as prepare_users
    init_db()
    catalog = [model_id for model_id in MODEL_IDS if not any(word in model_id.lower() for word in model_gateway._NON_CHAT)]
    snapshot = {'models': [{'model_id': model_id, 'provider': 'synthetic'} for model_id in MODEL_IDS],
                'fetched_at': datetime.now(timezone.utc).isoformat(), 'stale': False, 'gateway_reachable': True}
    model_gateway.chat_models = lambda force=False: list(catalog)
    model_gateway.gateway_directory = lambda force=False: dict(snapshot)
    model_access.chat_models = model_gateway.chat_models
    model_governance.chat_models = model_gateway.chat_models
    model_governance.gateway_directory = model_gateway.gateway_directory
    models.chat_models = model_gateway.chat_models
    with SessionLocal() as db:
        for model_id in MODEL_IDS:
            db.add(GatewayModelEntry(model_id=model_id, provider='synthetic', seen_at=datetime.now(timezone.utc)))
            db.add(ModelMetadata(model_id=model_id, purpose='chat', enabled=model_id not in DISABLED,
                                 display_name='益惠GLM-5.3' if model_id == 'e-glm-5.3' else None))
        agent = Agent(name='模型网关验收助手', agent_type='dialog', model_config_name='e-glm-5.3', config={}, is_active=True)
        db.add(agent)
        db.commit()
        agent_versions.baseline(db, agent.id)
    import sys
    sys.argv = ['fixture', 'setup', '/tmp/modelgateway52-identity.json']
    prepare_users()
    @asynccontextmanager
    async def lifespan(application):
        yield
    app.router.lifespan_context = lifespan
    return app
