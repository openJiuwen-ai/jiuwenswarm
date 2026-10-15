using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Windows.Automation;
using UIA = NativeUIA;

namespace ComputerUse.Windows {
    // One inspect, one cache. Full references keep execution possible; the
    // retained objects never acquire a session-long snapshot of property values.
    internal sealed class NativeReadCache {
        readonly UIA.IUIAutomationCacheRequest elementCache;
        readonly HashSet<int> cachedProperties;
        readonly Action check;
        readonly Action<string> note;
        readonly Dictionary<string, UiaElement> snapshots = new Dictionary<string, UiaElement>();
        readonly HashSet<string> uncached = new HashSet<string>();
        readonly Dictionary<string, UiaClient.Pattern[]> patterns = new Dictionary<string, UiaClient.Pattern[]>();
        sealed class ByReference : IEqualityComparer<UiaElement> {
            public bool Equals(UiaElement a, UiaElement b) { return Object.ReferenceEquals(a, b); }
            public int GetHashCode(UiaElement e) { return RuntimeHelpers.GetHashCode(e); }
        }
        readonly Dictionary<UiaElement, string> keys = new Dictionary<UiaElement, string>(new ByReference());
        string Key(UiaElement e) {
            string key; if (keys.TryGetValue(e, out key)) return key;
            check();
            try { key = NativeReadStore.Runtime(e); }
            catch (ElementNotAvailableException) { check(); key = null; }
            if (String.IsNullOrEmpty(key)) key = NativeReadStore.Token("unidentified");
            keys[e] = key; return key;
        }
        internal NativeReadCache(IEnumerable<string> fields, Action check, Action<string> note) {
            this.check = check; this.note = note;
            var requested = new HashSet<int> { AutomationElement.RuntimeIdProperty.Id,
                AutomationElement.NativeWindowHandleProperty.Id, AutomationElement.IsPasswordProperty.Id };
            foreach (string name in fields) {
                int p;
                if (NativeReadSchema.Properties.TryGetValue(name, out p)) requested.Add(p);
            }
            cachedProperties = requested;
            elementCache = UiaClient.Cache(requested.Concat(UiaClient.Patterns.Select(p => p.Available)));
        }
        internal UiaElement Snapshot(UiaElement element) {
            check(); string id = Key(element); UiaElement result;
            if (snapshots.TryGetValue(id, out result)) return result;
            try { check(); result = element.Snapshot(elementCache); check(); }
            catch (Exception error) {
                check(); result = element; uncached.Add(id);
                note("UIA property cache unavailable; bounded individual property reads used: " + error.Message);
            }
            snapshots[id] = result; return result;
        }
        internal object Get(UiaElement element, int property) {
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
        internal UiaClient.Pattern[] Patterns(UiaElement element) {
            check(); string id = Key(element); UiaClient.Pattern[] result;
            if (patterns.TryGetValue(id, out result)) return result;
            var snapshot = Snapshot(element);
            result = snapshot.GetSupportedPatterns(!uncached.Contains(id)); check(); patterns[id] = result; return result;
        }
        internal IEnumerable<UiaElement> Children(UiaElement element) {
            UiaElement[] children = null; bool fetched = false;
            try {
                check(); var cache = elementCache.Clone(); cache.TreeScope = (UIA.TreeScope)2;
                children = element.Snapshot(cache).CachedChildren; check(); fetched = true;
            } catch (Exception error) {
                check(); note("UIA children cache unavailable; bounded RawViewWalker used: " + error.Message);
            }
            if (fetched) {
                for (int i = 0; children != null && i < children.Length; i++) {
                    check(); var child = children[i]; string id = Key(child);
                    if (!snapshots.ContainsKey(id)) snapshots[id] = child;
                    yield return child;
                }
                yield break;
            }
            check(); var current = UiaClient.FirstChild(element);
            while (current != null) { check(); yield return current; check(); current = UiaClient.NextSibling(current); }
        }
    }
}
