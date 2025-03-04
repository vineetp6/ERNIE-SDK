# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import (
    AsyncIterator,
    Final,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from erniebot_agent.agents.agent import Agent
from erniebot_agent.agents.callback.callback_manager import CallbackManager
from erniebot_agent.agents.callback.handlers.base import CallbackHandler
from erniebot_agent.agents.schema import (
    DEFAULT_FINISH_STEP,
    AgentResponse,
    AgentStep,
    EndInfo,
    EndStep,
    PluginStep,
    ToolInfo,
    ToolStep,
)
from erniebot_agent.chat_models.erniebot import BaseERNIEBot
from erniebot_agent.file import File, FileManager
from erniebot_agent.memory import Memory
from erniebot_agent.memory.messages import (
    AIMessage,
    FunctionMessage,
    HumanMessage,
    Message,
)
from erniebot_agent.tools.base import BaseTool
from erniebot_agent.tools.tool_manager import ToolManager

_MAX_STEPS: Final[int] = 5
_logger = logging.getLogger(__name__)


class FunctionAgent(Agent):
    """An agent driven by function calling.

    The orchestration capabilities of a function agent are powered by the
    function calling ability of LLMs. Typically, a function agent asks the LLM
    to generate a response that can be parsed into an action (e.g., calling a
    tool with given arguments), and then the agent takes that action, which
    forms an agent step. The agent repeats this process until the maximum number
    of steps is reached or the LLM considers the task finished.

    Attributes:
        llm: The LLM that the agent uses.
        memory: The message storage that keeps the chat history.
        max_steps: The maximum number of steps in each agent run.
    """

    llm: BaseERNIEBot
    memory: Memory
    max_steps: int

    def __init__(
        self,
        llm: BaseERNIEBot,
        tools: Union[ToolManager, Iterable[BaseTool]],
        *,
        memory: Optional[Memory] = None,
        system: Optional[str] = None,
        callbacks: Optional[Union[CallbackManager, Iterable[CallbackHandler]]] = None,
        file_manager: Optional[FileManager] = None,
        plugins: Optional[List[str]] = None,
        max_steps: Optional[int] = None,
        first_tools: Optional[Sequence[BaseTool]] = [],
    ) -> None:
        """Initialize a function agent.

        Args:
            llm: An LLM for the agent to use.
            tools: A list of tools for the agent to use.
            memory: A memory object that equips the agent to remember chat
                history. If `None`, a `WholeMemory` object will be used.
            system: A message that tells the LLM how to interpret the
                conversations. If `None`, the system message contained in
                `memory` will be used.
            callbacks: A list of callback handlers for the agent to use. If
                `None`, a default list of callbacks will be used.
            file_manager: A file manager for the agent to interact with files.
                If `None`, a global file manager that can be shared among
                different components will be implicitly created and used.
            plugins: A list of names of the plugins for the agent to use. If
                `None`, the agent will use a default list of plugins. Set
                `plugins` to `[]` to disable the use of plugins.
            max_steps: The maximum number of steps in each agent run. If `None`,
                use a default value.
            first_tools: Tools scheduled to be called sequentially at the
                beginning of each agent run.

        Raises:
            ValueError: if `max_steps` is non-positive.
            RuntimeError: if tools in first_tools but not in tools list.

        """
        super().__init__(
            llm=llm,
            tools=tools,
            memory=memory,
            system=system,
            callbacks=callbacks,
            file_manager=file_manager,
            plugins=plugins,
        )
        if max_steps is not None:
            if max_steps <= 0:
                raise ValueError("Invalid `max_steps` value")
            self.max_steps = max_steps
        else:
            self.max_steps = _MAX_STEPS

        if first_tools:
            self._first_tools = first_tools
            for tool in self._first_tools:
                if tool not in self.get_tools():
                    raise RuntimeError("The tool in `first_tools` must be in the tools list.")
        else:
            self._first_tools = []

    async def _run(self, prompt: str, files: Optional[Sequence[File]] = None) -> AgentResponse:
        chat_history: List[Message] = []
        steps_taken: List[AgentStep] = []

        run_input = await HumanMessage.create_with_files(
            prompt, files or [], include_file_urls=self.file_needs_url
        )

        num_steps_taken = 0
        chat_history.append(run_input)

        for tool in self._first_tools:
            curr_step, new_messages = await self._call_first_tools(chat_history, selected_tool=tool)
            if not isinstance(curr_step, EndStep):
                chat_history.extend(new_messages)
                num_steps_taken += 1
                steps_taken.append(curr_step)
            else:
                # If tool choice not work, skip this round
                _logger.warning(f"Selected tool [{tool.tool_name}] not work")

        while num_steps_taken < self.max_steps:
            curr_step, new_messages = await self._step(chat_history)
            chat_history.extend(new_messages)
            if isinstance(curr_step, ToolStep):
                steps_taken.append(curr_step)

            elif isinstance(curr_step, PluginStep):
                steps_taken.append(curr_step)
                # 预留 调用了Plugin之后不结束的接口

                # 此处为调用了Plugin之后直接结束的Plugin
                curr_step = DEFAULT_FINISH_STEP

            if isinstance(curr_step, EndStep):
                response = self._create_finished_response(chat_history, steps_taken, curr_step)
                self.memory.add_message(chat_history[0])
                self.memory.add_message(chat_history[-1])
                return response
            num_steps_taken += 1
        response = self._create_stopped_response(chat_history, steps_taken)
        return response

    async def _call_first_tools(
        self, chat_history: List[Message], selected_tool: Optional[BaseTool] = None
    ) -> Tuple[AgentStep, List[Message]]:
        input_messages = self.memory.get_messages() + chat_history
        if selected_tool is None:
            llm_resp = await self.run_llm(messages=input_messages)
            return await self._process_step(llm_resp, chat_history)

        tool_choice = {"type": "function", "function": {"name": selected_tool.tool_name}}
        llm_resp = await self.run_llm(
            messages=input_messages,
            functions=[selected_tool.function_call_schema()],  # only regist one tool
            tool_choice=tool_choice,
        )
        return await self._process_step(llm_resp, chat_history)

    async def _step(self, chat_history: List[Message]) -> Tuple[AgentStep, List[Message]]:
        """Run a step of the agent.
        Args:
            chat_history: The chat history to provide to the agent.
        Returns:
            A tuple of an agent step and a list of new messages.
        """
        input_messages = self.memory.get_messages() + chat_history
        llm_resp = await self.run_llm(messages=input_messages)
        return await self._process_step(llm_resp, chat_history)

    async def _step_stream(
        self, chat_history: List[Message]
    ) -> AsyncIterator[Tuple[AgentStep, List[Message]]]:
        """Run a step of the agent in streaming mode.
        Args:
            chat_history: The chat history to provide to the agent.
        Returns:
            An async iterator that yields a tuple of an agent step and a list ofnew messages.
        """
        input_messages = self.memory.get_messages() + chat_history
        async for llm_resp in self.run_llm_stream(messages=input_messages):
            yield await self._process_step(llm_resp, chat_history)

    async def _run_stream(
        self, prompt: str, files: Optional[Sequence[File]] = None
    ) -> AsyncIterator[Tuple[AgentStep, List[Message]]]:
        """Run the agent with the given prompt and files in streaming mode.
        Args:
            prompt: The prompt for the agent to run.
            files: A list of files for the agent to use. If `None`, use an empty
                list.
        Returns:
            If `stream` is `False`, an agent response object. If `stream` is
            `True`, an async iterator that yields agent steps one by one.
        """
        chat_history: List[Message] = []
        steps_taken: List[AgentStep] = []

        run_input = await HumanMessage.create_with_files(
            prompt, files or [], include_file_urls=self.file_needs_url
        )

        num_steps_taken = 0
        chat_history.append(run_input)

        for tool in self._first_tools:
            curr_step, new_messages = await self._call_first_tools(chat_history, selected_tool=tool)
            if not isinstance(curr_step, EndStep):
                chat_history.extend(new_messages)
                num_steps_taken += 1
                steps_taken.append(curr_step)
            else:
                # If tool choice not work, skip this round
                _logger.warning(f"Selected tool [{tool.tool_name}] not work")

        is_finished = False
        new_messages = []
        end_step_msgs = []
        while is_finished is False:
            # IMPORTANT~! We use following code to get the response from LLM
            # When finish_reason is fuction_call, run_llm_stream return all info in one step, but
            # When finish_reason is normal chat, run_llm_stream return info in multiple steps.
            async for curr_step, new_messages in self._step_stream(chat_history):
                if isinstance(curr_step, ToolStep):
                    steps_taken.append(curr_step)
                    yield curr_step, new_messages

                elif isinstance(curr_step, PluginStep):
                    steps_taken.append(curr_step)
                    # 预留 调用了Plugin之后不结束的接口

                    # 此处为调用了Plugin之后直接结束的Plugin
                    curr_step = DEFAULT_FINISH_STEP
                    yield curr_step, new_messages

                elif isinstance(curr_step, EndStep):
                    is_finished = True
                    end_step_msgs.extend(new_messages)
                    yield curr_step, new_messages
                else:
                    raise RuntimeError("Invalid step type")
            chat_history.extend(new_messages)

        self.memory.add_message(run_input)
        end_step_msg = AIMessage(content="".join([item.content for item in end_step_msgs]))
        self.memory.add_message(end_step_msg)

    async def _process_step(self, llm_resp, chat_history) -> Tuple[AgentStep, List[Message]]:
        """Process and execute a step of the agent from LLM response.
        Args:
            llm_resp: The LLM response to convert.
            chat_history: The chat history to provide to the agent.
        Returns:
            A tuple of an agent step and a list of new messages.
        """
        new_messages: List[Message] = []
        output_message = llm_resp.message  # AIMessage
        new_messages.append(output_message)
        # handle function call
        if output_message.function_call is not None:
            tool_name = output_message.function_call["name"]
            tool_args = output_message.function_call["arguments"]
            tool_resp = await self.run_tool(tool_name=tool_name, tool_args=tool_args)
            new_messages.append(FunctionMessage(name=tool_name, content=tool_resp.json))
            return (
                ToolStep(
                    info=ToolInfo(tool_name=tool_name, tool_args=tool_args),
                    result=tool_resp.json,
                    input_files=tool_resp.input_files,
                    output_files=tool_resp.output_files,
                ),
                new_messages,
            )
        # handle plugin info with input/output files
        elif output_message.plugin_info is not None:
            file_manager = self.get_file_manager()
            return (
                PluginStep(
                    info=output_message.plugin_info,
                    result=output_message.content,
                    input_files=file_manager.sniff_and_extract_files_from_text(
                        chat_history[-1].content
                    ),  # TODO: make sure this is correct.
                    output_files=file_manager.sniff_and_extract_files_from_text(output_message.content),
                ),
                new_messages,
            )
        else:
            if output_message.clarify:
                # `clarify` and [`function_call`, `plugin`(directly end)] will not appear at the same time
                return EndStep(info=EndInfo(end_reason="CLARIFY"), result=None), new_messages
            return DEFAULT_FINISH_STEP, new_messages

    def _create_finished_response(
        self,
        chat_history: List[Message],
        steps: List[AgentStep],
        curr_step: EndStep,
    ) -> AgentResponse:
        last_message = chat_history[-1]
        return AgentResponse(
            text=last_message.content,
            chat_history=chat_history,
            steps=steps,
            status=curr_step.info["end_reason"],
        )

    def _create_stopped_response(
        self,
        chat_history: List[Message],
        steps: List[AgentStep],
    ) -> AgentResponse:
        return AgentResponse(
            text="Agent run stopped early.",
            chat_history=chat_history,
            steps=steps,
            status="STOPPED",
        )
