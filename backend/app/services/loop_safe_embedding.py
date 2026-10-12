from copy import copy

import httpx
from agentscope.embedding import OpenAIEmbeddingModel


class LoopSafeOpenAIEmbeddingModel(OpenAIEmbeddingModel):
    async def _call_api(self, inputs, **kwargs):
        request_model = copy(self)
        request_model.client = self.client.with_options(http_client=httpx.AsyncClient())
        async with request_model.client:
            return await OpenAIEmbeddingModel._call_api(request_model, inputs, **kwargs)
