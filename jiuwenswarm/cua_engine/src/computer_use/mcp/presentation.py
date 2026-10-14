from .._identity import WeakIdentityMap
from .._json import dumps, utf8
from ..observation.snapshot import snapshotDiff
from ..observation.coverage import confirmsRemoval


class ObservationPresenter:
    def __init__(self,maxBytes=4*1024*1024,maxScopes=32):
        self.maxBytes,self.maxScopes=maxBytes,maxScopes
        self._baselines,self._sources={},WeakIdentityMap()
        self._nextSource=self._bytes=0
    def key(self,observation):
        id_=self._sources.get(observation["source"])
        if id_ is None:
            self._nextSource+=1;id_=self._nextSource
            self._sources.set(observation["source"],id_)
        return dumps([id_,observation["scope"]])
    def invalidate(self,source=None):
        for key,baseline in list(self._baselines.items()):
            if source is None or baseline["source"] is source:
                self._bytes-=baseline["bytes"];self._baselines.pop(key)
    def text(self,observation):
        previous=self._baselines.get(self.key(observation))
        if observation["full"] or observation["view"]!="window" or previous is None or previous["view"]!="window":return observation["state"]["text"]
        notObserved={ref for ref in previous["refs"] if ref not in observation["refs"] and not confirmsRemoval(previous.get("coverage"),observation.get("coverage"),ref)}
        return snapshotDiff(previous,dict(text=observation["state"]["text"],refs=observation["refs"],contextRefs=observation.get("contextRefs",{})),notObserved)
    def delivered(self,observation):
        key=self.key(observation)
        previous=self._baselines.pop(key,None)
        if previous is not None:self._bytes-=previous["bytes"]
        size=len(utf8(observation["state"]["text"]))+len(utf8(dumps(observation["refs"])))+len(utf8(dumps(observation.get("coverage"))))
        if size>self.maxBytes:return
        while self._baselines and (len(self._baselines)>=self.maxScopes or self._bytes+size>self.maxBytes):
            self._bytes-=self._baselines.pop(next(iter(self._baselines)))["bytes"]
        self._baselines[key]=dict(source=observation["source"],scope=observation["scope"],view=observation["view"],text=observation["state"]["text"],refs=observation["refs"],coverage=observation.get("coverage"),bytes=size)
        self._bytes+=size


class ObservationOutput:
    def __init__(self,presenter,incremental=True):
        self.presenter,self.incremental=presenter,incremental
        self._pending,self._printed={},set()
    def observe(self,observation):
        key=self.presenter.key(observation)
        if key not in self._pending and len(self._pending)>=32:raise RuntimeError("Too many observation scopes in one call; inspect the collected results before reading more.")
        previous=self._pending.get(key)
        if previous is not None:self._printed.discard(id(previous))
        self._pending[key]=observation
    def invalidate(self,source):
        self.presenter.invalidate(source);self.discardPending(source)
    def discardPending(self,source):
        for key,observation in list(self._pending.items()):
            if observation["source"] is source:self._pending.pop(key);self._printed.discard(id(observation))
    def printed(self,text):
        matches=[v for v in self._pending.values() if v["state"]["text"]==text]
        if len(matches)!=1:return None
        self.presenter.delivered(matches[0]);self._printed.add(id(matches[0]))
        return matches[0]
    def flush(self,emit,emitImage=None):
        for observation in self._pending.values():
            if id(observation) not in self._printed:
                emit(self.presenter.text(observation) if self.incremental else observation["state"]["text"],observation)
                self.presenter.delivered(observation);self._printed.add(id(observation))
            if observation["view"]=="desktop" and observation["state"].get("screenshot") is not None and emitImage is not None:emitImage(observation["state"]["screenshot"])
        self._pending.clear();self._printed.clear()
