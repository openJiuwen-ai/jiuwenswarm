"""Bounded observations; refreshes never manufacture completed turns."""
from dataclasses import dataclass, field
from datetime import datetime
from collections import OrderedDict


@dataclass
class SessionState:
    trace: list = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {'context': {}, 'rateLimits': []})
    model: str | None = None
    turns: int | None = None
    updated: str = '尚未读取'
    observed: str = '等待本轮完成'
    history: list = field(default_factory=list)
    seen: OrderedDict = field(default_factory=OrderedDict)
    count: int = 0
    messages: list = field(default_factory=list)
    messages_loaded: bool = False
    expanded: bool = False
    error: bool = False
    history_error: bool = False
    panel_open: bool = False
    mounted: bool = False
    resources: object | None = None

    def observe(self, update, usage):
        if not update.request_id or update.request_id in self.seen:
            return
        values = (update.input_tokens, update.output_tokens, update.total_tokens, update.cache_tokens)
        if any(type(value) is not int or value < 0 for value in values):
            return
        self.seen[update.request_id] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        self.count += 1
        self.observed = datetime.now().astimezone().strftime('%H:%M:%S')
        self.history.append(dict(number=self.count, input=update.input_tokens, output=update.output_tokens,
            cache=update.cache_tokens, context=usage.get('context', {}).get('tokens')))
        self.history = self.history[-12:]
