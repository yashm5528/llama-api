from typing import Dict
from llama_api.schemas.models import ExllamaModel, LlamaCppModel

# ================== LLaMA.cpp models ================== #
llama = LlamaCppModel(
    model_path="TheBloke/Llama-2-7B-Chat-GGUF",  # automatic download
    max_total_tokens=2048
)
