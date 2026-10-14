handles=frozenset(("app","window","element","find"))
reads=frozenset(("read","readText","listApps"))


def inspectionComputer(computer):
    class Inspection:
        def __init__(self,target):object.__setattr__(self,"_target",target)
        def __getattr__(self,key):
            value=getattr(object.__getattribute__(self,"_target"),key)
            if not callable(value):return value
            def call(*args,**kwargs):
                if key in handles:return Inspection(value(*args,**kwargs))
                if key not in reads:raise RuntimeError(f"Compile exploration is read-only ({key}). Put input in the candidate or case.prepare and run learning.check(). No action dispatched.")
                return value(*args,**kwargs)
            return call
    return Inspection(computer)
