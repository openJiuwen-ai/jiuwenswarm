"""健康顾问安全边界 Rail。

检测高危关键词（胸痛、持续失眠、躯体疼痛、自杀/自伤想法等），
在每次模型调用前注入安全边界规则，检测到高危信号时注入紧急安全提示，
引导用户寻求专业医疗/心理帮助。
"""

from typing import Any, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail


HIGH_RISK_KEYWORDS: list[str] = [
    # 胸痛 / 心脏相关
    "胸痛", "胸闷", "心绞痛", "chest pain",
    # 持续失眠 / 严重睡眠问题
    "持续失眠", "长期失眠", "彻夜不眠", "连续失眠",
    # 躯体疼痛
    "躯体疼痛", "剧烈疼痛", "持续疼痛", "不明原因疼痛",
    # 自杀 / 自伤
    "自杀", "自伤", "自残", "不想活", "不想活了", "想死",
    "了结自己", "结束生命", "活不下去",
    "suicide", "self-harm", "kill myself",
    # 精神危机
    "重度抑郁", "幻觉", "妄想",
]

SAFETY_BOUNDARY_PROMPT = """## 健康顾问安全边界（强制执行，不可违反）

你是健康生活顾问，不是医生、营养师或心理咨询师。以下安全边界必须严格执行：

1. 禁止疾病诊断：不得根据症状诊断疾病、不得判病、不得开具药方、不得给出用药指导。用户描述身体不适时，引导就医。
2. 不替代专业人员：不替代营养师、医生、心理咨询师、精神科医生。
3. 保健品红线：不做保健品疗效承诺，不推荐保健品。
4. 方案原则：拒绝激进、高强度目标，坚持小步迭代、循序渐进。
5. 高危信号处理：检测到胸痛、持续失眠、躯体疼痛、自杀/自伤等关键词时，必须立即停止健康建议，优先输出安全提示，引导用户寻求专业医疗/心理帮助。提供紧急联系方式：心理援助热线 400-161-9995、急救 120。"""


class SafetyGuardRail(DeepAgentRail):
    """健康顾问安全边界 Rail：注入安全规则，检测高危关键词，触发紧急提示。"""

    def __init__(self) -> None:
        super().__init__()
        self._agent: Optional[Any] = None

    def init(self, agent: Any) -> None:
        """注册到 agent 时缓存运行时对象。"""
        self._agent = agent

    def uninit(self, agent: Any) -> None:
        self._agent = None

    @staticmethod
    def _detect_high_risk(text: str) -> list[str]:
        """检测文本中的高危关键词。"""
        if not text:
            return []
        text_lower = text.lower()
        return [kw for kw in HIGH_RISK_KEYWORDS if kw.lower() in text_lower]

    @staticmethod
    def _get_latest_user_text(ctx: AgentCallbackContext) -> str:
        """从上下文中提取最近一条用户消息文本。"""
        messages = getattr(ctx, "messages", None)
        if not messages:
            return ""
        for msg in reversed(messages):
            role = getattr(msg, "role", None)
            if role is None and isinstance(msg, dict):
                role = msg.get("role")
            if role != "user":
                continue
            content = getattr(msg, "content", None)
            if content is None and isinstance(msg, dict):
                content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        texts.append(part)
                return " ".join(texts)
        return ""

    @staticmethod
    def _inject_prompt(
        ctx: AgentCallbackContext, content: str, title: str
    ) -> None:
        """向 prompt 装配器注入 system prompt section。"""
        assembler = getattr(ctx, "prompt_assembler", None) or getattr(
            ctx, "prompt", None
        )
        if assembler is None:
            return
        add_section = getattr(assembler, "add_section", None)
        if callable(add_section):
            try:
                add_section(title=title, content=content)
            except TypeError:
                add_section(title, content)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """注入安全边界 prompt；检测到高危关键词时注入紧急提示。"""
        self._inject_prompt(
            ctx, SAFETY_BOUNDARY_PROMPT, "健康顾问安全边界"
        )
        user_text = self._get_latest_user_text(ctx)
        matched = self._detect_high_risk(user_text)
        if matched:
            emergency = (
                "## ⚠️ 高危信号检测 — 紧急安全指令\n\n"
                f"检测到高危关键词：{', '.join(matched)}。\n\n"
                "你必须立即执行以下操作：\n"
                "1. 停止所有健康生活建议\n"
                "2. 优先输出安全提示，引导用户寻求专业医疗/心理帮助\n"
                "3. 提供紧急联系方式：心理援助热线 400-161-9995、急救 120\n"
                "4. 不得对症状进行任何诊断或分析\n"
                "5. 语气关怀但不恐慌，让用户感到被关注和支持\n"
            )
            self._inject_prompt(ctx, emergency, "高危信号紧急提示")

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """清理本 rail 注入的临时状态。"""
        return
