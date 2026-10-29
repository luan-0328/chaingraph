"""Agent 的 LLM 客户端：唯一创建模型对象的地方。

两个入口：
    get_llm()                    普通对话模型（OpenAI 兼容，本项目用 DeepSeek）
    get_structured_llm(schema)   绑定了结构化输出 schema 的模型
    resolve_structured_llm(schema) 当前**生效**的结构化输出实现（默认同上，
                                 并发验收测试会把它换成确定性桩，见 stub_llm.py）

关于结构化输出（v2.md §18）：
    "Agent 不应该让 LLM 随意返回一段文本作为流程控制依据。"
    所以 analyze 节点用 with_structured_output(AgentDecision) 拿到 Pydantic 对象，
    generate_result 节点用 with_structured_output(AgentResult)。
    DeepSeek 支持 JSON Output / Tool Calls，langchain-openai 会自动选合适的方式。
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.core.config import settings
from app.core.exceptions import AgentExecutionError
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_llm() -> ChatOpenAI:
    """创建并缓存对话模型（参数全部来自 .env）。"""
    if not settings.llm_configured:
        raise AgentExecutionError(
            "未配置 LLM_API_KEY，Agent 无法运行（业务接口不受影响）",
            code="LLM_NOT_CONFIGURED",
        )

    logger.info(
        "初始化 LLM：model=%s base_url=%s thinking=%s",
        settings.llm_model,
        settings.llm_base_url,
        "disabled" if settings.llm_disable_thinking else "enabled",
    )

    # DeepSeek 通过 extra_body 传 thinking 开关（详见官方「思考模式」文档）
    extra_body = {"thinking": {"type": "disabled"}} if settings.llm_disable_thinking else None

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=2,
        extra_body=extra_body,
    )


@lru_cache(maxsize=8)
def get_structured_llm(schema: type[BaseModel]) -> ChatOpenAI:
    """绑定结构化输出的模型（按 schema 缓存，避免每轮都重建）。

    ⚠️ 这里必须指定 method="function_calling"：
        langchain-openai 默认用 `response_format: json_schema`（OpenAI 的严格模式），
        而 DeepSeek **不支持** 这种 response_format，会直接返回
        400 "This response_format type is unavailable now"。
        DeepSeek 支持的是 Tool Calls（function calling）与 JSON Output，
        所以走 function_calling 最稳；换成别家 OpenAI 兼容服务时也通用。
    """
    llm = get_llm()
    structured = llm.with_structured_output(schema, method="function_calling")
    logger.debug("绑定结构化输出：%s（method=function_calling）", schema.__name__)
    return structured


# ----------------------------------------------------------------------
# 结构化输出的可替换实现（只有一个用途：并发验收测试）
# ----------------------------------------------------------------------
# 并发测试要断言"10 个并发请求只产生 1 次执行"，就必须让每次执行的耗时可控 ——
# 真实 LLM 一次调用几百毫秒到几秒，并发窗口不稳定，测出来的结论也就不可信。
# 所以这里留一个可替换的工厂：默认（没配 AGENT_STUB_LLM=1）就是真实模型，
# 测试实例上换成确定性桩（app/agent/stub_llm.py），图的行为完全不变，
# 只是把"模型想几秒"换成"固定 sleep N 秒"。
_structured_llm_factory = get_structured_llm


def set_structured_llm_factory(factory) -> None:  # noqa: ANN001
    """替换结构化输出工厂（测试用）。传 None 恢复真实模型。"""
    global _structured_llm_factory
    _structured_llm_factory = factory or get_structured_llm
    logger.warning(
        "结构化输出实现已被替换为 %s",
        getattr(_structured_llm_factory, "__name__", _structured_llm_factory),
    )


def resolve_structured_llm(schema: type[BaseModel]):  # noqa: ANN202
    """取当前生效的结构化输出实现（桩或真实模型）。"""
    return _structured_llm_factory(schema)


async def ainvoke_structured(
    schema: type[BaseModel],
    messages: list[dict],
    *,
    what: str,
    retries: int = 1,
) -> BaseModel:
    """调用结构化输出，并处理"模型返回 None"这个真实故障。

    为什么必须有这个函数（实测踩过）：
        function_calling 模式下，供应商偶发只回一段普通文本、不带 tool_call，
        此时 with_structured_output 返回的是 **None 而不是抛异常**。
        节点里紧接着 `result.model_dump()` 就会炸成
        AttributeError: 'NoneType' object has no attribute 'model_dump'，
        整条链路变成 500，错误信息对排查毫无帮助（只知道 NoneType）。
        实测：DeepSeek 在 ~10 次调用里出现过一次空响应。

    策略：空响应通常是瞬时抖动 → 先重试；仍然为空才抛错，
    让工单落到 FAILED 并带上人能看懂的原因（而不是莫名其妙的 AttributeError）。
    注意只对"返回 None"重试：真正的异常（401/超时）交给调用方去包装，
    免得把一次鉴权失败重试成两次无用请求。
    """
    for attempt in range(retries + 1):
        result = await resolve_structured_llm(schema).ainvoke(messages)
        if result is not None:
            return result
        logger.warning("%s：第 %s 次返回空结果（结构化输出为 None）", what, attempt + 1)

    raise RuntimeError(f"模型未返回结构化结果（已重试 {retries} 次，通常是供应商返回了空响应）")
