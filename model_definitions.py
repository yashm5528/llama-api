from llama_api.utils.path import suppress_import_error


with suppress_import_error():
    from llama_api.modules.llama_cpp import LlamaCppModel

    # ================== LLaMA.cpp models ================== #
    orca_mini_3b = LlamaCppModel(
        model_path="orca-mini-3b.ggmlv3.q4_1.bin",  # model_path here
        max_total_tokens=4096,
        rope_freq_base=26000,
        rope_freq_scale=0.5,
    )

with suppress_import_error():
    from llama_api.modules.exllama import ExllamaModel

    # ================== ExLLaMa models ================== #
    orca_mini_7b = ExllamaModel(
        model_path="orca_mini_7b",  # model_path here
        max_total_tokens=4096,
        compress_pos_emb=2.0,
    )


# Define a mapping from OpenAI model names to LLaMA models.
# e.g. If you request API model "gpt-3.5-turbo",
# the API will load the LLaMA model "orca_mini_3b"
openai_replacement_models: dict[str, str] = {
    "gpt-3.5-turbo": "orca_mini_3b",
    "gpt-4": "orca_mini_7b",
}
