# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import asyncio
import copy
import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List, Tuple, Union

from pydantic import BaseModel

from openjiuwen.core.workflow import WorkflowCard as WorkflowSchema, WorkflowCard
from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.legacy.schema import PluginSchema
from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig

from openjiuwen.core.session import Config
from openjiuwen.core.session.agent import Session, create_agent_session
from openjiuwen.core.session.stream import CustomSchema
from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.workflow import Workflow, generate_workflow_key

if TYPE_CHECKING:
    pass



class WorkflowFactory:
    """Workflow factory class that creates a new workflow instance on each call (concurrency-safe).

    Usage:
        # Method 1: Use decorator (recommended, most concise)
        @workflow_provider(workflow_id="my_workflow", workflow_version="1.0")
        def create_workflow():
            return Workflow()  # No need to set metadata

        single_agent.add_workflows([create_workflow])

        # Method 2: Direct instantiation
        provider = WorkflowFactory("my_workflow", "1.0", lambda: build_workflow())
        single_agent.add_workflows([provider])

    Features:
        - Callable: provider() returns a new workflow instance each time
        - Provides id/version attributes for workflow key generation
        - Auto-sets workflow metadata on each call
    """

    def __init__(
            self,
            workflow_id: str,
            workflow_version: str,
            factory: Callable[[], Workflow],
            workflow_name: str = '',
            workflow_description: str = '',
            input_schema=None,
    ):
        """
        Args:
            workflow_id: Workflow ID for registration
            workflow_version: Workflow version for registration
            factory: Factory function that returns a new Workflow instance on each call
        """
        self._factory = factory
        self._workflow_card = WorkflowCard(id=workflow_id, name=workflow_name, description=workflow_description,
                                           version=workflow_version, input_params=input_schema)

    def card(self):
        return self._workflow_card

    def __call__(self):
        """Return a new workflow instance on each call, with metadata auto-set.

        Supports both sync and async factory functions:
        - Sync factory: returns Workflow directly
        - Async factory: returns coroutine that resolves to Workflow
        """
        result = self._factory()

        # Handle async factory (returns coroutine)
        if asyncio.iscoroutine(result) or inspect.iscoroutinefunction(self._factory):
            async def async_wrapper():
                workflow = await result if asyncio.iscoroutine(result) else await self._factory()
                return workflow

            return async_wrapper()
        return result


def workflow_provider(workflow_id: str, workflow_version: str, workflow_name: str = '', workflow_description: str = '',
                      inputs: Union[dict, BaseModel] = None):
    """Decorator to create a WorkflowFactory from a factory function.

    Usage:
        @workflow_provider(workflow_id="weather_workflow", workflow_version="1.0")
        def create_weather_workflow():
            flow = Workflow()
            # ... build workflow ...
            return flow

        single_agent.add_workflows([create_weather_workflow])

    Args:
        workflow_id: Workflow ID for registration
        workflow_version: Workflow version for registration

    Returns:
        Decorator that wraps a factory function as WorkflowFactory
    """

    def decorator(func: Callable[[], Workflow]) -> WorkflowFactory:
        return WorkflowFactory(workflow_id, workflow_version, func, workflow_name, workflow_description, inputs)

    return decorator


class BaseAgent(ABC):
    """Base Agent - Minimal interface definition (new architecture)
    """

    def __init__(self, agent_config):
        """Initialize Agent
        
        Args:
            agent_config: Agent configuration
        """
        # 1. Create Config wrapper (backward compatible)
        self._config_wrapper = Config()
        self._config_wrapper.set_agent_config(agent_config)
        self.agent_config = agent_config
        self._config = self._config_wrapper  # Unified interface

        # 3. Create ContextEngine
        self._context_engine = self._create_context_engine()

        # 4. Uniformly hold tools and workflows (eliminate subclass duplication)
        self._tools: List[Tool] = []
        self._workflows: List[Workflow] = []

    def config(self) -> Config:
        """Get Config wrapper - Backward compatible method interface
        
        Returns:
            Config instance (contains get_agent_config() method)
        """
        return self._config_wrapper

    @property
    def tools(self) -> List[Tool]:
        """Get tools list - Read-only access for subclasses"""
        return self._tools

    @property
    def workflows(self) -> List[Workflow]:
        """Get workflows list - Read-only access for subclasses"""
        return self._workflows

    @property
    def context_engine(self) -> ContextEngine:
        """Get Context Engine - Unified public interface"""
        return self._context_engine

    def _create_context_engine(self) -> ContextEngine:
        """Create ContextEngine - Internal method, called during base class initialization"""
        # Get max conversation rounds configuration
        if (hasattr(self.agent_config, 'constrain') and
                hasattr(self.agent_config.constrain, 'reserved_max_chat_rounds')):
            max_rounds = self.agent_config.constrain.reserved_max_chat_rounds
        else:
            max_rounds = 10  # Default value

        context_config = ContextEngineConfig(
            max_context_message_num=max_rounds * 2
        )
        return ContextEngine(
            config=context_config,
        )

    @abstractmethod
    async def invoke(self, inputs: Dict, session: Session = None) -> Dict:
        """Synchronous invocation entry point - Abstract method
        
        Subclasses must implement this method
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement invoke() method"
        )

    @abstractmethod
    async def stream(self, inputs: Dict, session: Session = None) -> AsyncIterator[Any]:
        """Streaming invocation entry point - Abstract method
        
        Subclasses must implement this method
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement stream() method"
        )

    # ===== Dynamic configuration interface (Plan A: Backward compatible) =====

    def add_prompt(self, prompt_template: List[Dict]) -> None:
        """Add Prompt template
        
        Args:
            prompt_template: Prompt template list, format like
                [{"role": "system", "content": "..."}]
        
        Note:
        - This method only updates configuration, does not affect already created session
        - Subclasses should override this method if they need to sync session
        """
        # Check if configuration has prompt_template field
        if hasattr(self.agent_config, 'prompt_template'):
            # Append mode: Keep original prompt, add new prompt
            self.agent_config.prompt_template.extend(prompt_template)
        else:
            config_class_name = self.agent_config.__class__.__name__
            logger.warning(
                f"{config_class_name} has no prompt_template field, "
                "add_prompt operation ignored"
            )

    def add_tools(self, tools: List[Tool]) -> None:
        """Add tools (update config, session, and self._tools simultaneously)
        
        Args:
            tools: List of tool instances
        """

        for tool in tools:
            # 1. Add tool name to config.tools
            if tool.card.name not in self.agent_config.tools:
                self.agent_config.tools.append(tool.card.name)

            # 2. Generate PluginSchema (if configuration supports)
            if hasattr(self.agent_config, 'plugins'):
                # Check if already exists
                existing_plugin_names = {
                    p.name for p in self.agent_config.plugins
                }
                if tool.card.name not in existing_plugin_names:
                    plugin_schema = self._tool_to_plugin_schema(tool)
                    self.agent_config.plugins.append(plugin_schema)

            # 3. Add to self._tools (avoid duplication)
            existing_tool_names = {t.card.name for t in self._tools}
            if tool.card.name not in existing_tool_names:
                self._tools.append(tool)
            from openjiuwen.core.runner import Runner
            # 4. Sync to session (auto register)
            Runner.resource_mgr.add_tool(tool=[tool], tag=self.agent_config.id)

    def add_workflows(
            self,
            workflows: List[Union[Workflow, Callable[[], Workflow]]]
    ) -> None:
        """Add workflows (update config and session simultaneously).
        
        Supports three registration methods:
        1. Workflow instance - registered directly (note: does not support concurrent calls)
        2. WorkflowFactory object - registered directly (concurrency-safe, recommended)
        3. Callable with id/version attributes - async/sync provider (concurrency-safe)
        
        Args:
            workflows: List of workflow instances or WorkflowFactory/provider objects
        
        Concurrency Notes:
            - Instance: multiple conversations share the same instance, not concurrency-safe
            - WorkflowFactory or provider with id/version: new instance on each get_workflow()
            
        Recommended Usage (concurrent scenarios):
            # Method 1: Use @workflow_provider decorator (most concise)
            @workflow_provider(workflow_id="my_wf", workflow_version="1.0")
            def create_workflow():
                return Workflow()
            single_agent.add_workflows([create_workflow])

            # Method 2: Use WorkflowFactory directly
            provider = WorkflowFactory("my_wf", "1.0", lambda: build_workflow())
            single_agent.add_workflows([provider])

            # Method 3: Async provider with id/version attributes
            async def _create_provider(wf, mgr):
                async def provider():
                    return await wf.compile(mgr)
                provider.id = wf.id
                provider.version = wf.version
                return provider
            providers = [await _create_provider(wf, mgr) for wf in workflows]
            single_agent.add_workflows(providers)
        """
        logger.info(f"BaseAgent.add_workflows called with {len(workflows)} workflows")

        def make_workflow_provider(workflow):
            """Create a provider function that returns the workflow instance."""
            def provider():
                return workflow
            return provider

        for item in workflows:
            # Extract workflow_id, workflow_version, and provider/workflow
            workflow_card = None
            provider = None
            is_provider = True
            if isinstance(item, WorkflowFactory):
                # WorkflowFactory object: use id/version attributes
                provider = item
                workflow_card = provider.card()
            elif callable(item) and hasattr(item, 'id') and hasattr(item, 'version'):
                # Callable with id/version attributes (preferred way for async providers)
                provider = item
                workflow_card = WorkflowCard(id=getattr(item, 'id'),
                                             name=getattr(item, 'name', None),
                                             description=getattr(item, 'description', None),
                                             version=getattr(item, 'version'),
                                             input_params=getattr(item, "input_params", None) or
                                                          getattr(item, "inputs", None))
            elif callable(item):
                # Bare callable without id/version: error
                raise ValueError(
                    f"Callable workflow provider must have 'id' and 'version' attributes. "
                    f"Use @workflow_provider decorator or WorkflowFactory class."
                )
            else:
                # Create a provider function to wrap the instance
                provider = make_workflow_provider(item)
                workflow_card = item.card
                is_provider = False

            workflow_key = generate_workflow_key(workflow_card.id, workflow_card.version)

            # Check if already exists
            existing_keys = {
                generate_workflow_key(w.id, w.version)
                for w in self.agent_config.workflows
            }
            logger.info(
                f"Workflow {workflow_key}: existing_keys={existing_keys}, "
                f"exists={workflow_key in existing_keys}, is_provider={is_provider}"
            )

            # Even if schema exists, still need to add workflow
            if workflow_key not in existing_keys:
                # 1. Update config.workflows
                workflow_schema = WorkflowSchema(
                    id=workflow_card.id,
                    name=workflow_card.name,
                    version=workflow_card.version,
                    description=workflow_card.description or "",
                    input_params=workflow_card.input_params
                )

                self.agent_config.workflows.append(workflow_schema)

            # 2. Sync to session (provider or instance)
            to_register = provider

            # 3. Also add to global resource_mgr (for cross-session access)
            try:
                logger.info(f"Adding workflow {'provider' if is_provider else 'instance'} "
                            f"{workflow_key} to global resource_mgr")
                from openjiuwen.core.runner import Runner
                workflow_card_copy = copy.deepcopy(workflow_card)
                workflow_card_copy.id = workflow_key
                Runner.resource_mgr.add_workflow(card=workflow_card_copy, workflow=to_register,
                                                 tag=self.agent_config.id)
                logger.info(f"Successfully added workflow {'provider' if is_provider else 'instance'} {workflow_key}")
            except Exception as e:
                logger.error(f"Failed to add workflow to global resource_mgr: {e}")

    def remove_workflows(
            self,
            workflows: List[Tuple[str, str]]
    ) -> None:
        """Remove workflows from single_agent (update config and session simultaneously).
        
        Removes workflows from three locations:
        1. agent_config.workflows (WorkflowSchema list)
        2. session workflow manager
        3. global resource_mgr (if available)
        
        Args:
            workflows: List of (workflow_id, workflow_version) tuples to remove
            
        Example:
            single_agent.remove_workflows([
                ("my_workflow", "1.0"),
                ("another_workflow", "2.0")
            ])
        """
        logger.info(f"BaseAgent.remove_workflows called with {len(workflows)} workflows")

        for workflow_id, workflow_version in workflows:
            workflow_key = generate_workflow_key(workflow_id, workflow_version)
            logger.info(f"Removing workflow: {workflow_key}")

            # 1. Remove from agent_config.workflows
            original_count = len(self.agent_config.workflows)
            self.agent_config.workflows = [
                w for w in self.agent_config.workflows
                if not (w.id == workflow_id and w.version == workflow_version)
            ]
            removed_from_config = original_count - len(self.agent_config.workflows)
            logger.info(f"Removed {removed_from_config} workflow schema(s) from config")

            # 3. Remove from global resource_mgr
            try:
                from openjiuwen.core.runner import Runner
                Runner.resource_mgr.remove_workflow(workflow_key)
                logger.info(f"Successfully removed workflow {workflow_key} from global resource_mgr")
            except Exception as e:
                logger.error(f"Failed to remove workflow from global resource_mgr: {e}")

    def bind_workflows(self, workflows: List[Workflow]) -> None:
        """Bind workflows - Backward compatible alias method
        
        Args:
            workflows: List of workflow instances
        """
        self.add_workflows(workflows)

    def add_plugins(self, plugins: List) -> None:
        """Add plugin Schema
        
        Args:
            plugins: PluginSchema list
        
        Note:
        - This method only updates plugins field in configuration
        - Subclasses should override this method if they need to sync session
        """
        if hasattr(self.agent_config, 'plugins'):
            # Check duplication
            existing_names = {p.name for p in self.agent_config.plugins}
            for plugin in plugins:
                if plugin.name not in existing_names:
                    self.agent_config.plugins.append(plugin)
                    existing_names.add(plugin.name)
        else:
            config_class_name = self.agent_config.__class__.__name__
            logger.warning(
                f"{config_class_name} has no plugins field, "
                "add_plugins operation ignored"
            )

    def _tool_to_plugin_schema(self, tool: Tool):
        """Convert Tool instance to PluginSchema
        
        This is an internal method for automatically generating plugin schema
        
        Args:
            tool: Tool instance
            
        Returns:
            PluginSchema: Plugin schema object
        """
        # Generate inputs from tool.params
        inputs = {
            "type": "object",
            "properties": {},
            "required": []
        }

        if hasattr(tool, 'params') and tool.params:
            for param in tool.params:
                prop = {
                    "type": param.type,
                    "description": param.description
                }
                inputs["properties"][param.name] = prop
                if param.required:
                    inputs["required"].append(param.name)

        tool_description = ""
        if hasattr(tool, 'description'):
            tool_description = tool.description

        return PluginSchema(
            id=tool.card.id,
            name=tool.card.name,
            description=tool_description,
            inputs=inputs
        )

    async def clear_session(self, session_id: str = "default_session"):
        await self._session.release(session_id)


class ControllerAgent(BaseAgent):
    """Agent that holds Controller (new architecture)
    """

    def __init__(self, agent_config, controller=None):
        """Initialize ControllerAgent
        
        Args:
            agent_config: Agent configuration
            controller: Optional Controller instance (will be auto-configured)
            
        Note:
            If controller is provided, it will be automatically configured with
            config, context_engine and session from this single_agent via setup_from_agent()
            
        Usage:
            # Simplest way - controller auto-configured:
            controller = WorkflowController()  # No parameters needed
            single_agent = ControllerAgent(config=config, controller=controller)
            
            # Alternative - set controller after single_agent creation:
            single_agent = ControllerAgent(config=config)
            single_agent.controller = WorkflowController()  # Will be auto-configured
        """
        super().__init__(agent_config)
        self.controller = controller

        # Auto-configure controller if provided
        if self.controller is not None:
            self._setup_controller()

    def _setup_controller(self):
        """Setup controller with single_agent's config, context_engine and session"""
        if hasattr(self.controller, 'setup_from_agent'):
            self.controller.setup_from_agent(self)

    @property
    def controller(self):
        """Get controller"""
        return self._controller

    @controller.setter
    def controller(self, value):
        """Set controller and auto-configure it"""
        self._controller = value
        # Auto-configure when setting controller
        # Only if single_agent is already initialized (has _context_engine)
        if value is not None and hasattr(self, '_context_engine'):
            self._setup_controller()

    async def invoke(self, inputs: Dict, session: Session = None) -> Dict:
        """Synchronous invocation - Fully delegate to controller
        
        Args:
            inputs: Input data
            session: Session instance (if None, auto create)
        
        Returns:
            Execution result
        """
        if not self.controller:
            raise RuntimeError(
                f"{self.__class__.__name__} has no controller, "
                "subclass should create controller before invocation"
            )

        # If session not provided, create one
        session_id = inputs.get("conversation_id", "default_session")
        if session is None:
            from openjiuwen.core.single_agent import AgentCard
            agent_session = create_agent_session(session_id=session_id, card=AgentCard(id=self.agent_config.id))
            await agent_session.pre_run(inputs=inputs)
        else:
            if isinstance(session, Session):
                agent_session = getattr(session, "_inner")
            else:
                agent_session = session
        await self.context_engine.create_context(session=agent_session)
        try:
            # Fully delegate to controller
            result = await self.controller.invoke(inputs, agent_session)
            if session is None:
                await self.context_engine.save_contexts(agent_session)
                await agent_session.post_run()

            return result
        except Exception as e:
            await agent_session.post_run()
            raise

    async def stream(self, inputs: Dict, session: Session = None) -> AsyncIterator[Any]:
        """Streaming invocation - Fully delegate to controller
        
        Args:
            inputs: Input data
            session: Session instance (if None, auto create)
        
        Yields:
            Streaming output
        
        Note:
            When external session is provided, data is written to it but not read
            from stream_iterator (to avoid nested read deadlock). External caller
            reads stream data from session.
        """
        if not self.controller:
            raise RuntimeError(
                f"{self.__class__.__name__} has no controller, "
                "subclass should create controller before invocation"
            )

        # If session not provided, create one
        session_id = inputs.get("conversation_id", "default_session")
        if session is None:
            from openjiuwen.core.single_agent import AgentCard
            agent_session = create_agent_session(session_id=session_id, card=AgentCard(id=self.agent_config.id))
            await agent_session.pre_run(inputs=inputs)
            need_cleanup = True
            own_stream = True  # Owns stream lifecycle
        else:
            if isinstance(session, Session):
                agent_session = getattr(session, "_inner")
            else:
                agent_session = session
            need_cleanup = False
            own_stream = False  # External owns stream lifecycle

            # Sync single_agent's tools to external session
            # When external session is provided, single_agent's tools need to be registered
            from openjiuwen.core.runner import Runner
            if self._tools:
                tools_to_add = [(tool.card.name, tool) for tool in self._tools]
                Runner.resource_mgr.add_tool(tool=tools_to_add, tag=self.agent_config.id)
            # Sync agent's workflows to external session
            # When external session is provided, agent's workflows need to be registered
        # Store final result for send_to_agent
        final_result_holder = {"result": None}
        await self.context_engine.create_context(session=agent_session)

        # Fully delegate to controller
        async def stream_process():
            try:
                res = await self.controller.invoke(inputs, agent_session)
                final_result_holder["result"] = res
                # Interrupt: list contains __interaction__ OutputSchema
                # Only WorkflowController writes to session here
                # Other controllers (e.g. HierarchicalMainController) forward
                # lower single_agent results, which already wrote to shared session
                from openjiuwen.core.application.workflow_agent.workflow_controller import (
                    WorkflowController
                )
                if isinstance(res, list) and isinstance(self.controller, WorkflowController):
                    for item in res:
                        if isinstance(item, CustomSchema):
                            await agent_session.write_custom_stream(item)
                        else:
                            await agent_session.write_stream(item)
            finally:
                if need_cleanup:
                    await self.context_engine.save_contexts(agent_session)
                    await agent_session.post_run()

        task = asyncio.create_task(stream_process())

        if own_stream:
            # Read from stream_iterator only when owning stream
            # External caller reads if external session provided
            async for result in agent_session.stream_iterator():
                yield result

        await task

        # When own_stream=False, yield final result to send_to_agent
        # so send_to_agent can get single_agent's actual return value
        if not own_stream and final_result_holder["result"] is not None:
            res = final_result_holder["result"]
            if isinstance(res, list):
                # Interrupt: return list (contains __interaction__)
                for item in res:
                    yield item
            else:
                # Normal completion: yield dict or other result
                yield res

    async def clear_session(self, session_id: str = "default_session"):
        await self._session.release(session_id)
        self.context_engine.clear_context(session_id=session_id)
        await self.controller.cleanup_conversation(session_id)
