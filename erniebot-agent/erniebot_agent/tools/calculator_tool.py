from __future__ import annotations

from typing import Dict, List, Type

from erniebot_agent.message import AIMessage, HumanMessage, Message
from erniebot_agent.tools.schema import ToolParameterView
from pydantic import Field

from .base import Tool


class CalculatorToolInputView(ToolParameterView):
    math_formula: str = Field(description='标准的数学公式，例如："2+3"、"3 - 4 * 6", "(3 + 4) * (6 + 4)" 等。 ')


class CalculatorToolOutputView(ToolParameterView):
    formula_result: float = Field(description="数学公式计算的结果")


class CalculatorTool(Tool):
    description: str = "CalculatorTool用于执行数学公式计算"
    input_type: Type[ToolParameterView] = CalculatorToolInputView
    ouptut_type: Type[ToolParameterView] = CalculatorToolOutputView

    async def __call__(self, math_formula: str) -> Dict[str, float]:
        return {"formula_result": eval(math_formula)}

    @property
    def examples(self) -> List[Message]:
        return [
            HumanMessage("请告诉我三加六等于多少？"),
            AIMessage(
                None,
                function_call={
                    "name": self.tool_name,
                    "thoughts": f"用户想知道3加6等于多少，我可以使用{self.tool_name}工具来计算公式，其中`math_formula`字段的内容为：'3+6'。",
                    "arguments": '{"math_formula": "3+6"}',
                },
            ),
            HumanMessage("一加八再乘以5是多少？"),
            AIMessage(
                None,
                function_call={
                    "name": self.tool_name,
                    "thoughts": f"用户想知道1加8再乘5等于多少，我可以使用{self.tool_name}工具来计算公式，"
                    "其中`math_formula`字段的内容为：'(1+8)*5'。",
                    "arguments": '{"math_formula": "(1+8)*5"}',
                },
            ),
            HumanMessage("我想知道十二除以四再加五等于多少？"),
            AIMessage(
                None,
                function_call={
                    "name": self.tool_name,
                    "thoughts": f"用户想知道12除以4再加5等于多少，我可以使用{self.tool_name}工具来计算公式，"
                    "其中`math_formula`字段的内容为：'12/4+5'。",
                    "arguments": '{"math_formula": "12/4+5"}',
                },
            ),
        ]
