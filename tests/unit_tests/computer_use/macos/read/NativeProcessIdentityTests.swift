import Foundation

@main struct NativeProcessIdentityTests {
    @MainActor static func main() {
        typealias Identity = NativeProcessIdentity
        // Metadata probes never send input or start an application. Successful
        // reads incur no diagnostic callback; failure keeps its actual syscall.
        var failure: Identity.ReadFailure?
        let own = Identity.read(ProcessInfo.processInfo.processIdentifier, onFailure: { failure = $0 })
        precondition(own != nil && failure == nil)
        precondition(Identity.read(-1, onFailure: { failure = $0 }) == nil)
        precondition(failure?.operation == "proc_pidinfo" && failure?.result != failure?.expectedBytes)
        precondition(failure?.error != 0)
        let bundle = URL(fileURLWithPath: "/Applications/Example.app")
        func process(_ pid: Int32, _ parent: Int32, _ time: UInt64, _ name: String = "helper") -> Identity {
            Identity(pid: pid, parent: parent, seconds: time, microseconds: 1, executable: bundle.path + "/Contents/MacOS/" + name)
        }
        let root=process(100,1,10,"main"), relay=process(101,100,11), child=process(102,101,12)
        let adopted=process(102,1,12)
        precondition(child == adopted && Set([child,adopted]).count == 1)
        precondition(child != process(102,1,13) && child != process(102,1,12,"replacement"))
        var live: [Int32:Identity] = [100:root,101:relay,102:child]
        let registry=NativeProcessOwnership()
        let belongs = { (value: Identity) in registry.belongs(value,to:root,bundle:bundle,read:{live[$0]}) }
        precondition(belongs(child))
        live[101]=nil; live[102]=adopted
        precondition(belongs(child) && belongs(adopted))
        // New descendants can be linked through a previously confirmed member.
        let grandchild=process(103,102,14); live[103]=grandchild
        precondition(belongs(grandchild))
        let fresh=NativeProcessOwnership()
        precondition(!fresh.belongs(adopted,to:root,bundle:bundle,read:{live[$0]}))
        // Missing metadata refuses now without forgetting proof for later.
        live[102]=nil
        registry.prune(read:{live[$0]},isGone:{_ in false})
        precondition(!belongs(child)); live[102]=adopted
        precondition(belongs(child))
        // A later process with the same PID never inherits this proof.
        live[102]=process(102,1,20)
        precondition(!belongs(child) && !belongs(live[102]!))
        live[102]=process(102,1,12,"replacement")
        precondition(!belongs(child) && !belongs(live[102]!))
        live[102]=adopted; live[100]=process(100,1,30,"main")
        precondition(!belongs(adopted))
        precondition(!registry.belongs(adopted,to:live[100]!,bundle:bundle,read:{live[$0]}))
        live[100]=nil; precondition(!belongs(adopted))
        live[100]=root
        // Confirmed disappearance removes a claim, rather than using it later.
        live[102]=nil; registry.prune(read:{live[$0]},isGone:{$0 == 102})
        live[102]=adopted; precondition(!belongs(adopted))
        // Path prefixes, cycles, and a reused intermediary are not ancestry.
        let outside=Identity(pid:104,parent:100,seconds:12,microseconds:1,executable:"/Applications/Example.app.extra/helper")
        live[104]=outside; precondition(!belongs(outside))
        live[101]=process(101,102,11); live[102]=child
        precondition(!belongs(child))
        live[101]=process(101,100,40)
        precondition(!belongs(child))
        // Discovery cannot authorize a process that changes during the walk.
        live[101]=relay
        var reads=0
        precondition(!fresh.belongs(child,to:root,bundle:bundle,read:{ pid in
            if pid == child.pid { reads += 1; return reads == 1 ? child : process(102,1,50) }
            return live[pid]
        }))
        for depth in 1...17 {
            let pid=Int32(200+depth), parent: Int32 = depth == 1 ? root.pid : pid-1
            live[pid]=process(pid,parent,UInt64(100+depth))
        }
        precondition(NativeProcessOwnership().belongs(live[216]!,to:root,bundle:bundle,read:{live[$0]}))
        precondition(!NativeProcessOwnership().belongs(live[217]!,to:root,bundle:bundle,read:{live[$0]}))
        print("PASS process identity and provenance: reparenting, descendants, metadata recovery, PID/exec replacement, owner exit, cycles and discovery races")
    }
}
