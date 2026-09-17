import os

import pytest


@pytest.fixture(autouse=True)
def llm_env_clean():
    saved = {k: os.environ.get(k) for k in
             ("LLM_BASE_URL", "LLM_MODEL", "ASR_BASE_URL", "ASR_MODEL")}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
