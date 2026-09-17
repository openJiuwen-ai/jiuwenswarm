# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import uuid
from enum import Enum
from typing import List, Optional, Union, Dict, Any, TYPE_CHECKING
from pydantic import BaseModel, Field

from openjiuwen.core.controller.schema.dataframe import DataFrame, TextDataFrame, JsonDataFrame

if TYPE_CHECKING:
    from openjiuwen.core.controller.schema.task import Task


class EventType(str, Enum):
    """Event Type Enumeration

    Defines all supported event types:
    - INPUT: User input event
    - TASK_INTERACTION: Task interaction event (requires user interaction during task execution)
    - TASK_COMPLETION: Task completion event
    - TASK_FAILED: Task failed event
    """
    INPUT = "input"
    TASK_INTERACTION = "task_interaction",
    TASK_COMPLETION = "task_completion",
    TASK_FAILED = "task_failed"


class Event(BaseModel):
    """Event Base Class

    Base class for all events, containing event type, event ID, and metadata.
    """
    event_type: EventType
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        """Post-initialization processing

        Ensures metadata is not None.
        """
        if self.metadata is None:
            self.metadata = {}


class InputEvent(Event):
    """Input Event

    User input event containing input data.
    This is the main input type for the controller, used to receive user requests.

    Attributes:
        event_type: Event type, fixed as EventType.INPUT
        input_data: Input data list, supports text, file, and JSON formats
    """
    event_type: EventType = EventType.INPUT
    input_data: List[DataFrame] = Field(default_factory=list)

    @classmethod
    def from_user_input(cls, user_input: Union[str, dict, 'InputEvent']) -> "InputEvent":
        """Create input event from user input

        Convenience method to convert user input to InputEvent.

        Args:
            user_input: User input, supports string, dictionary, and InputEvent

        Returns:
            InputEvent: Input event object
        """
        if isinstance(user_input, cls):
            return user_input

        if isinstance(user_input, str):
            return cls(
                event_type=EventType.INPUT,
                input_data=[TextDataFrame(text=user_input)]
            )
        if isinstance(user_input, dict):
            return cls(
                event_type=EventType.INPUT,
                input_data=[JsonDataFrame(data=user_input)]
            )

        raise TypeError(f"Unsupported user input type: {type(user_input)}. Must be str, dict, or InputEvent.")


class TaskInteractionEvent(Event):
    """Task Interaction Event

    Event generated when user interaction is required during task execution.
    This event is generated when a task requires the user to provide additional information or confirmation.

    Attributes:
        event_type: Event type, fixed as EventType.TASK_INTERACTION
        interaction: Interaction content list, containing information that requires user interaction
        task: Associated task object
    """
    event_type: EventType = EventType.TASK_INTERACTION
    interaction: List[DataFrame] = Field(default_factory=list)
    task: Optional[Task] = None


class TaskCompletionEvent(Event):
    """Task Completion Event

    Event generated when task execution is completed, containing task results.
    This event is generated when a task successfully completes and includes the task's output results.

    Attributes:
        event_type: Event type, fixed as EventType.TASK_COMPLETION
        task_result: Task result list, containing the task's output data
        task: Associated task object
    """
    event_type: EventType = EventType.TASK_COMPLETION
    task_result: List[DataFrame] = Field(default_factory=list)
    task: Optional[Task] = None


class TaskFailedEvent(Event):
    """Task Failed Event

    Event generated when task execution fails, containing error information.
    This event is generated when an error occurs during task execution and includes error details.

    Attributes:
        event_type: Event type, fixed as EventType.TASK_FAILED
        error_message: Error message describing the reason for task failure
        task: Associated task object
    """
    event_type: EventType = EventType.TASK_FAILED
    error_message: Optional[str] = None
    task: Optional[Task] = None
