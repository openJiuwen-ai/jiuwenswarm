from .._identity import WeakIdentityMap
from .._json import dumps, utf8
from ..observation.coverage import confirms_removal
from ..observation.snapshot import snapshot_diff


class ObservationPresenter:

    def __init__(self, max_bytes=4 * 1024 * 1024, max_scopes=32):
        self.max_bytes, self.max_scopes = max_bytes, max_scopes
        self._baselines, self._sources = {}, WeakIdentityMap()
        self._next_source = self._bytes = 0

    def key(self, observation):
        id_ = self._sources.get(observation["source"])
        if id_ is None:
            self._next_source += 1
            id_ = self._next_source
            self._sources.set(observation["source"], id_)
        return dumps([id_, observation["scope"]])

    def invalidate(self, source=None):
        for key, baseline in list(self._baselines.items()):
            if source is None or baseline["source"] is source:
                self._bytes -= baseline["bytes"]
                self._baselines.pop(key)

    def text(self, observation):
        previous = self._baselines.get(self.key(observation))
        requires_full_tree = observation["full"] or observation["view"] != "window" or previous is None or previous[
            "view"] != "window"
        if requires_full_tree:
            return observation["state"]["text"]
        not_observed = set()
        for ref in previous["refs"]:
            if ref not in observation["refs"]:
                removed = confirms_removal(previous.get("coverage"), observation.get("coverage"), ref)
                if not removed:
                    not_observed.add(ref)
        return snapshot_diff(
            previous,
            dict(text=observation["state"]["text"], refs=observation["refs"],
                 contextRefs=observation.get("contextRefs", {})), not_observed)

    def delivered(self, observation):
        key = self.key(observation)
        previous = self._baselines.pop(key, None)
        if previous is not None:
            self._bytes -= previous["bytes"]
        size = len(utf8(observation["state"]["text"])) + len(utf8(dumps(observation["refs"]))) + len(
            utf8(dumps(observation.get("coverage"))))
        if size > self.max_bytes:
            return
        while self._baselines and (len(self._baselines) >= self.max_scopes or self._bytes + size > self.max_bytes):
            self._bytes -= self._baselines.pop(next(iter(self._baselines)))["bytes"]
        self._baselines[key] = dict(source=observation["source"], scope=observation["scope"], view=observation["view"],
                                    text=observation["state"]["text"], refs=observation["refs"],
                                    coverage=observation.get("coverage"), bytes=size)
        self._bytes += size


class ObservationOutput:

    def __init__(self, presenter, incremental=True):
        self.presenter, self.incremental = presenter, incremental
        self._pending, self._printed = {}, set()

    def observe(self, observation):
        key = self.presenter.key(observation)
        if key not in self._pending and len(self._pending) >= 32:
            raise RuntimeError(
                "Too many observation scopes in one call; inspect the collected results before reading more.")
        previous = self._pending.get(key)
        if previous is not None:
            self._printed.discard(id(previous))
        self._pending[key] = observation

    def invalidate(self, source):
        self.presenter.invalidate(source)
        self.discard_pending(source)

    def discard_pending(self, source):
        for key, observation in list(self._pending.items()):
            if observation["source"] is source:
                self._pending.pop(key)
                self._printed.discard(id(observation))

    def printed(self, text):
        matches = [v for v in self._pending.values() if v["state"]["text"] == text]
        if len(matches) != 1:
            return None
        self.presenter.delivered(matches[0])
        self._printed.add(id(matches[0]))
        return matches[0]

    def flush(self, emit, emit_image=None):
        for observation in self._pending.values():
            if id(observation) not in self._printed:
                emit(
                    self.presenter.text(observation) if self.incremental else observation["state"]["text"], observation)
                self.presenter.delivered(observation)
                self._printed.add(id(observation))
            if observation["view"] == "desktop" and observation["state"].get(
                    "screenshot") is not None and emit_image is not None:
                emit_image(observation["state"]["screenshot"])
        self._pending.clear()
        self._printed.clear()
