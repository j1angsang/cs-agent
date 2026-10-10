"""内部 LLM 调用统一打标：只有面向用户的答案 token 进 SSE，内部调用必须带此标签。"""
INTERNAL_MODEL_TAG = "cs_internal_summary"
