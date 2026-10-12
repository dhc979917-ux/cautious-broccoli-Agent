"""Run on 52 in the backend image: PYTHONPATH=/src/backend python test/_test_nine_feedback.py."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from jose import jwt

from app.config import settings
from app.agents.architectures.dialog import ToolAssistantBuilder
from app.agents.delivery_profile import DeliveryKind, detect_from_text
from app.agents.model_factory import _parameters
from app.models.model_access_policy import ModelAccessPolicy
from app.services.context_compaction import _prompt_entry
from app.services.model_access import allowed_models, effective_model
from app.services.model_gateway import chat_models
from app.services import model_gateway, ocr_gateway


class FakeDb:
    def __init__(self, policies):
        self.policies = policies

    def get(self, model, key):
        if model is ModelAccessPolicy:
            return self.policies.get(key)
        return None


class GatewayFeedbackTests(unittest.TestCase):
    def test_user_policy_overrides_role_without_expanding_catalog(self):
        policies = {
            ("role", "viewer"): SimpleNamespace(allowed_models=["first"]),
            ("user", "7"): SimpleNamespace(allowed_models=["second", "deleted"]),
        }
        user = SimpleNamespace(id=7, role="viewer")
        db = FakeDb(policies)
        catalog = ["first", "second"]
        self.assertEqual(allowed_models(db, user, catalog), ["second"])
        with patch("app.services.model_access.chat_models", return_value=catalog):
            self.assertEqual(effective_model(db, user, SimpleNamespace(model_config_name="first")), "second")
            with self.assertRaises(HTTPException) as error:
                effective_model(db, user, SimpleNamespace(model_config_name="first"), "first")
            self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(allowed_models(FakeDb({}), user, catalog), catalog)

    def test_model_catalog_excludes_non_chat_models(self):
        model_gateway._catalog = (0, [])
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"id": "chat-a"}, {"id": "e-bge-m3-embedding"},
                                   {"id": "vl-ocr"}, {"id": "asr-1"}, {"id": "voice-tts"}]},
        )
        with patch("app.services.model_gateway.requests.get", return_value=response), patch(
            "app.services.model_gateway.headers", return_value={}
        ):
            self.assertEqual(chat_models(force=True), ["chat-a"])

    def test_model_parameters_are_openai_compatible(self):
        parameters = _parameters(ToolAssistantBuilder().get_parameters())
        self.assertEqual(parameters.max_tokens, 8192)
        self.assertNotIn("thinking_budget", type(parameters).model_fields)

    def test_platform_introduction_does_not_start_artifact_workflow(self):
        self.assertEqual(detect_from_text("介绍本平台有什么功能"), DeliveryKind.ANSWER)
        self.assertEqual(detect_from_text("为什么网页渲染失败？"), DeliveryKind.ANSWER)
        self.assertEqual(detect_from_text("做一个可交互的 HTML 页面"), DeliveryKind.ARTIFACT)

    def test_compaction_preserves_long_user_intent(self):
        user_entry = _prompt_entry({"role": "user", "content": "需求" * 6000})
        tool_entry = _prompt_entry({"role": "tool", "content": "日志" * 6000})
        self.assertGreater(len(user_entry), 10000)
        self.assertLess(len(tool_entry), 2400)

    def test_ocr_credential_cannot_be_used_as_user_jwt(self):
        token = ocr_gateway.mint_ocr_token(7, 99)
        claims = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        self.assertEqual((claims["kind"], claims["uid"], claims["sid"]), ("sandbox-ocr", 7, 99))
        request = SimpleNamespace(headers={"Authorization": "Bearer invalid"})
        with self.assertRaises(HTTPException) as error:
            asyncio.run(ocr_gateway.recognize_image(request))
        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
