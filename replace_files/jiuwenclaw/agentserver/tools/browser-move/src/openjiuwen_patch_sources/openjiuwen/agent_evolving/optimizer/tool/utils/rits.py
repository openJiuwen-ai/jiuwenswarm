# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
from tenacity import retry, stop_after_attempt, wait_random_exponential

from openjiuwen.core.foundation.llm import (
    ModelClientConfig,
    ModelRequestConfig
)
from openjiuwen.core.foundation.llm import OpenAIModelClient


def get_rits_response(*args, **kwargs):
    try:
        return rits_response(*args, **kwargs)
    except Exception as e:
        return {'error': f"Cannot complete LLM call. Error: {e}"}


@retry(wait=wait_random_exponential(min=1, max=5), stop=stop_after_attempt(2), reraise=True)
def rits_response(
        model_id: str, 
        prompt: str, 
        llm_api_key: str, 
        verify_fn=None, 
        verbose: bool = False,
        **kwargs
):
    model_config = ModelRequestConfig(
        model=model_id,
        temperature=1
    )
    model_client = ModelClientConfig(
        client_provider="OpenAI",
        api_base="https://api.openai.com/v1",
        api_key=llm_api_key,
        verify_ssl=False
    )
    client = OpenAIModelClient(
        model_config=model_config,
        model_client_config=model_client
    )

    response = asyncio.run(
        client.invoke(
            messages=[
                {
                    "role": "developer",
                    "content": prompt
                }
            ]      
        )
    )

    output = response.content
    if verify_fn is not None:
        output = verify_fn(output)
    return output

