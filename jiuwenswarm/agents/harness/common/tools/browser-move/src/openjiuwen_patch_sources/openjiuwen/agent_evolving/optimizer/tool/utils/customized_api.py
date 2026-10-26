# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import importlib.util
import json
import os
import sys
from typing import Any, Dict, Optional

from openjiuwen.core.common.logging import logger


class SimpleAPIWrapper:
    """
    Simplified version of BfclAPIWrapper to load custom functions and data.
    """
    
    def __init__(
            self, 
            tool_path: Optional[str] = None, 
            fn_call_name: str = None, 
            custom_functions: Optional[Dict] = None
        ):
        """
        Initialize the wrapper with either a Python file or custom functions.
        
        Args:
            tool_path: Path to Python file containing functions (optional)
            custom_functions: Dictionary of custom functions to use (optional)
        """
        self.functions = {}
        self.fn_call_name = fn_call_name
        self.module = None
        # Load from Python file if provided
        if tool_path and os.path.exists(tool_path):
            self._load_module(tool_path)
        
        # Add custom functions if provided
        if custom_functions:
            self.functions.update(custom_functions)

    def _load_module(self, tool_path: str):
        """Load functions from a Python module file"""
        module_name = os.path.splitext(os.path.basename(tool_path))[0]
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        self.module = module
        
        # Extract all callable functions from the module
        for name in dir(module):
            obj = getattr(module, name)
            if callable(obj) and not name.startswith('_'):
                self.functions[name] = obj
    
    def add_function(self, name: str, func: callable):
        """Add a custom function to the wrapper"""
        self.functions[name] = func
    
    def __call__(self, tool: Dict[str, Any], tool_input: Dict[str, Any]):
        """
        Execute a function call with the same interface as BfclAPIWrapper.
        
        Args:
            tool: Dictionary containing function info, e.g., {'function': {'name': 'func_name'}}
            tool_input: Dictionary containing function parameters
            
        Returns:
            Tuple of (json_response_string, status_code)
        """
        tool_name = tool['name']
        logger.info(f"=== Trying to execute tool: {tool}, tool_input: {tool_input} ===")

        params = tool_input
        fn = self.functions.get(self.fn_call_name)
        # Handle function not found
        if fn is None:
            logger.error(f"request invalid, no function '{tool_name}' found")
            return json.dumps({
                "error": f"request invalid, no function '{tool_name}' found", 
                "response": ""
            }), 12
        
        # Execute function
        try:
            output = fn(params)
            return json.dumps({'response': output}, ensure_ascii=False), 0
        except Exception as e:
            logger.error(f"request invalid, error: {str(e)}")
            return json.dumps({
                "error": f"request invalid, error: {str(e)}", 
                "response": ""
            }), 12


class SimpleAPIWrapperFromCallable:
    """
    Simplified version of BfclAPIWrapper to load custom functions and data.
    """
    
    def __init__(self, tool_callable, name: str, config: Dict):
        """
        Initialize the wrapper with a callable function and config.

        Args:
            tool_callable: The callable function to use
            config: Configuration dictionary
        """
        self.functions = {}
        self.fn_call_name = name
        self.functions[self.fn_call_name] = tool_callable
        self.module = None

    def __call__(self, tool: Dict[str, Any], tool_input: Dict[str, Any]):
        """
        Execute a function call with the same interface as BfclAPIWrapper.
        
        Args:
            tool: Dictionary containing function info, e.g., {'function': {'name': 'func_name'}}
            tool_input: Dictionary containing function parameters
            
        Returns:
            Tuple of (json_response_string, status_code)
        """
        tool_name = tool['name']
        logger.info(f"=== Trying to execute tool: {tool}, tool_input: {tool_input} ===")

        params = tool_input
        fn = self.functions.get(self.fn_call_name)
        # Handle function not found
        if fn is None:
            logger.error(f"request invalid, no function '{tool_name}' found")
            return json.dumps({
                "error": f"request invalid, no function '{tool_name}' found", 
                "response": ""
            }), 12
        
        # Execute function
        try:
            output = fn(params)
            return json.dumps({'response': output}, ensure_ascii=False), 0
        except Exception as e:
            logger.error(f"request invalid, error: {str(e)}")
            return json.dumps({
                "error": f"request invalid, error: {str(e)}", 
                "response": ""
            }), 12
        """
        Initialize the wrapper with either a Python file or custom functions.
        
        Args:
            tool_path: Path to Python file containing functions (optional)
            custom_functions: Dictionary of custom functions to use (optional)
        """
        self.functions = {}
        self.fn_call_name = fn_call_name

        # Load from Python file if provided
        if tool_path and os.path.exists(tool_path):
            self._load_module(tool_path)
        
        # Add custom functions if provided
        if custom_functions:
            self.functions.update(custom_functions)

    def _load_module(self, tool_path: str):
        """Load functions from a Python module file"""
        module_name = os.path.splitext(os.path.basename(tool_path))[0]
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        self.module = module
        
        # Extract all callable functions from the module
        for name in dir(module):
            obj = getattr(module, name)
            if callable(obj) and not name.startswith('_'):
                self.functions[name] = obj
    
    def add_function(self, name: str, func: callable):
        """Add a custom function to the wrapper"""
        self.functions[name] = func
    
    def __call__(self, tool: Dict[str, Any], tool_input: Dict[str, Any]):
        """
        Execute a function call with the same interface as BfclAPIWrapper.
        
        Args:
            tool: Dictionary containing function info, e.g., {'function': {'name': 'func_name'}}
            tool_input: Dictionary containing function parameters
            
        Returns:
            Tuple of (json_response_string, status_code)
        """
        tool_name = tool['name']
        logger.info(f"=== Trying to execute tool: {tool}, tool_input: {tool_input} ===")

        params = tool_input
        fn = self.functions.get(self.fn_call_name)
        # Handle function not found
        if fn is None:
            logger.error(f"request invalid, no function '{tool_name}' found")
            return json.dumps({
                "error": f"request invalid, no function '{tool_name}' found", 
                "response": ""
            }), 12
        
        # Execute function
        try:
            output = fn(params)
            return json.dumps({'response': output}, ensure_ascii=False), 0
        except Exception as e:
            logger.error(f"request invalid, error: {str(e)}")
            return json.dumps({
                "error": f"request invalid, error: {str(e)}", 
                "response": ""
            }), 12


def load_custom_data(data_path: str, api_wrapper: SimpleAPIWrapper):
    """
    Load function definitions from your custom data format.
    
    Args:
        data_path: Path to your data file (JSON, JSONL, etc.)
        api_wrapper: The API wrapper instance
        
    Returns:
        List of tool definitions
    """
    tools = []
    
    # Handle different file formats
    if data_path.endswith('.jsonl'):
        # JSONL format (like BFCL)
        with open(data_path, 'r') as f:
            for line in f:
                data = json.loads(line)
                if 'function' in data:
                    functions = data['function'] if isinstance(data['function'], list) else [data['function']]
                    for fn in functions:
                        tools.append({
                            "type": "function",
                            "function": fn
                        })
    
    elif data_path.endswith('.json'):
        # Single JSON file
        with open(data_path, 'r') as f:
            data = json.load(f)
            
            # Handle different JSON structures
            if isinstance(data, list):
                # List of function definitions
                for item in data:
                    if 'function' in item:
                        tools.append({
                            "type": "function",
                            "function": item['function']
                        })
                    else:
                        # Assume the item itself is a function definition
                        tools.append({
                            "type": "function",
                            "function": item
                        })
            elif 'functions' in data:
                # Object with functions array
                for fn in data['functions']:
                    tools.append({
                        "type": "function",
                        "function": fn
                    })
    
    return tools

