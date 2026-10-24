import Foundation

@main struct ApplicationInstancesTests {
    struct Lifetime: Hashable { let pid: Int; let incarnation: Int }
    static func main() {
        let registry = ApplicationInstances<Lifetime>()
        let first = Lifetime(pid: 42, incarnation: 1), second = Lifetime(pid: 43, incarnation: 1)
        let a = registry.id(for: first), b = registry.id(for: second)
        precondition(a != b && registry.id(for: first) == a)
        precondition(registry.instance(a) == first && registry.instance(b) == second)
        print("PASS: aliases of one native lifetime share an ID; independent lifetimes remain selectable")

        registry.retire { $0 == first }
        let replacement = Lifetime(pid: 42, incarnation: 2)
        let c = registry.id(for: replacement)
        precondition(c != a && registry.instance(a) == nil && registry.instance(c) == replacement)
        precondition(registry.instance(b) == second)
        let other = ApplicationInstances<Lifetime>()
        precondition(other.id(for: replacement) != c && other.instance(c) == nil)
        precondition(ApplicationInstances<Lifetime>.isInstanceSelector(a))
        precondition(!ApplicationInstances<Lifetime>.isInstanceSelector("/Applications/Test.app"))
        print("PASS: retired IDs, reused PIDs and foreign sessions cannot resolve to replacement instances")
    }
}
