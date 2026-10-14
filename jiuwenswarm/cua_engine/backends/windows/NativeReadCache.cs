using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    // One inspect, one cache. Full references keep execution possible; the
    // retained objects never acquire a session-long snapshot of property values.
    internal sealed class NativeReadCache {
        readonly CacheRequest elementCache;
        readonly HashSet<AutomationProperty> cachedProperties;
        readonly Action check;
        readonly Action<string> note;
        readonly Dictionary<string, AutomationElement> snapshots = new Dictionary<string, AutomationElement>();
        readonly HashSet<string> uncached = new HashSet<string>();
        readonly Dictionary<string, AutomationPattern[]> patterns = new Dictionary<string, AutomationPattern[]>();
        sealed class ByReference : IEqualityComparer<AutomationElement> {
            public bool Equals(AutomationElement a, AutomationElement b) { return Object.ReferenceEquals(a, b); }
            public int GetHashCode(AutomationElement e) { return RuntimeHelpers.GetHashCode(e); }
        }
        readonly Dictionary<AutomationElement, string> keys = new Dictionary<AutomationElement, string>(new ByReference());
        string Key(AutomationElement e) {
            string key; if (keys.TryGetValue(e, out key)) return key;
            check();
            try { key = NativeReadStore.Runtime(e); }
            catch (ElementNotAvailableException) { check(); key = null; }
            if (String.IsNullOrEmpty(key)) key = NativeReadStore.Token("unidentified");
            keys[e] = key; return key;
        }
        internal NativeReadCache(IEnumerable<string> fields, Action check, Action<string> note) {
            this.check = check; this.note = note;
            elementCache = new CacheRequest { TreeScope = TreeScope.Element,
                TreeFilter = Automation.RawViewCondition, AutomationElementMode = AutomationElementMode.Full };
            var requested = new HashSet<AutomationProperty> { AutomationElement.RuntimeIdProperty,
                AutomationElement.NativeWindowHandleProperty, AutomationElement.IsPasswordProperty };
            foreach (string name in fields) {
                AutomationProperty p;
                if (NativeReadSchema.Properties.TryGetValue(name, out p)) requested.Add(p);
            }
            cachedProperties = requested;
            foreach (var p in requested) elementCache.Add(p);
        }
        internal AutomationElement Snapshot(AutomationElement element) {
            check(); string id = Key(element); AutomationElement result;
            if (snapshots.TryGetValue(id, out result)) return result;
            try { check(); result = element.GetUpdatedCache(elementCache); check(); }
            catch (Exception error) {
                check(); result = element; uncached.Add(id);
                note("UIA property cache unavailable; bounded individual property reads used: " + error.Message);
            }
            snapshots[id] = result; return result;
        }
        internal object Get(AutomationElement element, AutomationProperty property) {
            var snapshot = Snapshot(element); check();
            object value;
            // Relations are not inserted into the property cache: their native
            // objects and scope must be handled explicitly by the collector.
            if (uncached.Contains(Key(element)) || !cachedProperties.Contains(property))
                value = element.GetCurrentPropertyValue(property, true);
            else {
                try { value = snapshot.GetCachedPropertyValue(property, true); }
                catch (InvalidOperationException) { check(); value = element.GetCurrentPropertyValue(property, true); }
            }
            check();
            if (ReferenceEquals(value, AutomationElement.NotSupported)) throw new NativeFieldUnavailable("unsupported");
            return value;
        }
        internal AutomationPattern[] Patterns(AutomationElement element) {
            check(); string id = Key(element); AutomationPattern[] result;
            if (patterns.TryGetValue(id, out result)) return result;
            result = element.GetSupportedPatterns(); check(); patterns[id] = result; return result;
        }
        internal IEnumerable<AutomationElement> Children(AutomationElement element) {
            AutomationElementCollection children = null; bool fetched = false;
            try {
                check(); var cache = elementCache.Clone(); cache.TreeScope = TreeScope.Children;
                children = element.GetUpdatedCache(cache).CachedChildren; check(); fetched = true;
            } catch (Exception error) {
                check(); note("UIA children cache unavailable; bounded RawViewWalker used: " + error.Message);
            }
            if (fetched) {
                for (int i = 0; children != null && i < children.Count; i++) {
                    check(); var child = children[i]; string id = Key(child);
                    if (!snapshots.ContainsKey(id)) snapshots[id] = child;
                    yield return child;
                }
                yield break;
            }
            check(); var walker = TreeWalker.RawViewWalker; var current = walker.GetFirstChild(element);
            while (current != null) { check(); yield return current; check(); current = walker.GetNextSibling(current); }
        }
    }
}
