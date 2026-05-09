"""
Prompts Loader — 统一加载各 agent 的 prompt 和配置
================================================

使用方式：
    from agent.prompts import get_prompts, get_config

    prompts = get_prompts("side_hustle")
    config = get_config("side_hustle")

未来扩展：
    - 添加 writing.py / search.py / scraper.py / coding.py
    - 添加数据库读取逻辑（覆盖 DEFAULT_CONFIG）
"""

from typing import TypedDict

from agent.prompts import side_hustle


class AgentPrompts(TypedDict):
    system: str
    plan: str
    execute: str
    reflect: str
    recommend: str


class AgentConfig(TypedDict):
    agent_type: str
    model_plan: str
    model_execute: str
    model_reflect: str
    model_recommend: str
    max_tokens: int
    max_plan_steps: int
    enabled: bool


# Agent 类型 → 对应的 module
_AGENT_MODULES = {
    "side_hustle": side_hustle,
    # TODO Phase 2: 添加其他 agent
    # "writing": writing,
    # "search": search,
    # "scraper": scraper,
    # "coding": coding,
}


def get_prompts(agent_type: str) -> AgentPrompts:
    """Load prompts for a given agent type."""
    module = _AGENT_MODULES.get(agent_type)
    if module is None:
        # Fallback: use side_hustle as default
        module = side_hustle
    return {
        "system": module.SYSTEM_PROMPT,
        "plan": module.PLAN_PROMPT,
        "execute": module.EXECUTE_PROMPT,
        "reflect": module.REFLECT_PROMPT,
        "recommend": module.RECOMMEND_PROMPT,
    }


def get_config(agent_type: str) -> AgentConfig:
    """
    Load config for an agent type.

    Phase 1 (current): Read from code constants only (DEFAULT_CONFIG).
    Phase 2 (future): Check database agent_configs table first, fallback to constants.
    """
    module = _AGENT_MODULES.get(agent_type)
    if module is None:
        module = side_hustle
    return dict(module.DEFAULT_CONFIG)  # copy so callers can't mutate


def list_agent_types() -> list[str]:
    """Return all registered agent types."""
    return list(_AGENT_MODULES.keys())
