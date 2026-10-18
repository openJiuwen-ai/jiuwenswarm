using System;
using UIA = NativeUIA;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    internal sealed class NativeFieldUnavailable : Exception {
        internal readonly string State;
        internal NativeFieldUnavailable(string state, string detail = "") : base(detail) { State = state; }
    }
    // Raw UIA properties and relations. Projection, menu selection and modal
    // following belong to Python; no observation calls v3 Backend.ReadApp.
    internal sealed class NativeReadBackend : INativeReadBackend {
        readonly string session;
        readonly NativeReadStore store = new NativeReadStore();
        readonly NativeRootProbe rootProbe = new NativeRootProbe();
        readonly NativeCapturedText texts = new NativeCapturedText();
        internal NativeReadStore Store { get { return store; } }
        internal bool NeedsRestart { get { return rootProbe.Blocked || NativeWindowDiscovery.RequiresRestart; } }
        sealed class Page { internal string Surface, Node, Property; internal string[] Targets; internal int Offset; }
        readonly Dictionary<string, Page> pages = new Dictionary<string, Page>();
        readonly Queue<string> pageOrder = new Queue<string>();
        // Owning containers are facts, not automatic traversal back into an
        // entire table or a source of input authority.
        static readonly string[] Relations = { "RawViewChildren", "RawViewParent", "LabeledBy", "GridItem.ContainingGrid", "SelectionItem.SelectionContainer" };
        Request request; double until;
        NativeReadCache cache;
        readonly Dictionary<string, NativeReadText> textReaders = new Dictionary<string, NativeReadText>();
        List<string> observationNotes;
        readonly Action<string> trace;
        internal NativeReadBackend(string session, Action<string> trace = null) { this.session = session; this.trace = trace; }
        void Trace(string value) { if (trace != null) trace(value); }
        public void Validate(Dictionary<string, object> target, Request r) { store.Validate(target, r, true); }
        public void Release() { cache = null; textReaders.Clear(); store.Release(); }
        static Dictionary<string, object> Stamp(double start) { return Wire.Obj("id", NativeReadStore.Token("observation"), "startedAt", Math.Floor(start), "completedAt", Math.Max(Math.Floor(start), Math.Ceiling(Wire.Now))); }
        void Check() { request.Check(); if (Wire.Now >= until) throw new NativeFieldUnavailable("timeout", "bounded UIA observation budget exhausted"); }
        object Get(UiaElement e, AutomationProperty property) { return Get(e, property.Id); }
        object Get(UiaElement e, int property) {
            Check(); object value = cache == null ? e.GetCurrentPropertyValue(property, true) : cache.Get(e, property); request.Check();
            if (ReferenceEquals(value, AutomationElement.NotSupported)) throw new NativeFieldUnavailable("unsupported");
            return value;
        }
        UiaClient.Pattern[] Patterns(UiaElement e) { Check(); var result = cache == null ? e.GetSupportedPatterns() : cache.Patterns(e); Check(); return result; }
        NativeReadText TextReader(NativeNodeRecord node) {
            // Availability was captured with the node's properties. Avoid
            // repeated cross-process GetCurrentPattern failures for every text
            // field/relation on controls that do not expose TextPattern.
            // Do not collect protected text. Unknown protection state remains
            // unknown, rather than being silently interpreted as false.
            object password = Get(node.Element, AutomationElement.IsPasswordProperty);
            if (!(password is bool)) throw new NativeFieldUnavailable("error", "UIA password protection state unavailable");
            if ((bool)password) throw new NativeFieldUnavailable("unsupported", "protected text is not captured");
            NativeReadText reader;
            if (!textReaders.TryGetValue(node.Id, out reader)) {
                var supported = Patterns(node.Element);
                textReaders[node.Id] = reader = new NativeReadText(node.Element,
                    supported.Any(p => p.Id == TextPattern.Pattern.Id), supported.Any(p => p.Id == ValuePattern.Pattern.Id), Check, trace);
            }
            return reader;
        }
        object Field(NativeNodeRecord node, string name, int textLimit = NativeCapturedText.PreviewUnits + 1) {
            Trace("field " + node.Id + " " + name);
            var e = node.Element;
            if (name == "Win32.ClientRectangle") {
                if (new IntPtr(Convert.ToInt64(Get(e, AutomationElement.NativeWindowHandleProperty))) != node.Window.Native.Handle)
                    throw new NativeFieldUnavailable("absent", "not the selected native window root");
                node.Window.Native.Validate();
                Native.RECT bounds; var origin = new Native.POINT();
                if (!Native.GetClientRect(node.Window.Native.Handle, out bounds) || !Native.ClientToScreen(node.Window.Native.Handle, ref origin))
                    throw new NativeFieldUnavailable("error", "native client rectangle unavailable");
                return Wire.Obj("x", origin.X, "y", origin.Y, "width", bounds.Right-bounds.Left, "height", bounds.Bottom-bounds.Top);
            }
            if (name == "UIA.ReadSource") return "IUIAutomation; RawView; native provider fragments; legacy HWND text may use an identity-checked read-only .NET proxy";
            if (name == "UIA.ReferenceScope") return node.RelationOnly ? "observedRelation; input requires independent live identity and window validation" : "nativeWindow";
            if (name == "UIA.Collection") {
                var patterns = Patterns(e);
                return Wire.Obj("view", "raw", "mayContainVirtualizedItems", patterns.Any(p => p.Id == ItemContainerPattern.Pattern.Id),
                    "isVirtualizedItem", patterns.Any(p => p.Id == VirtualizedItemPattern.Pattern.Id),
                    "logicalContentComplete", null, "detail", "Raw children describe exposed objects, not all logical items or pixels. No realization or scrolling performed.");
            }
            if (name.StartsWith("Text.", StringComparison.Ordinal)) return TextReader(node).Field(name, textLimit);
            RequirePattern(e, name);
            if (name == "Value.Value") {
                object password = Get(e, AutomationElement.IsPasswordProperty);
                if (!(password is bool) || (bool)password) throw new NativeFieldUnavailable("unsupported", "protected or unknown-protection text is not captured");
            }
            if (name == "SupportedPatterns") return Patterns(e).Select(p => p.ProgrammaticName).ToArray();
            int property;
            if (!NativeReadSchema.Properties.TryGetValue(name, out property)) throw new NativeFieldUnavailable("unsupported", "unknown UIA property");
            object value = Get(e, property);
            var controlType = value as ControlType;
            if (controlType != null) return controlType.ProgrammaticName;
            if (value is System.Windows.Rect) return NativeReadText.Bounds((System.Windows.Rect)value);
            if (value is double) return NativeReadValues.Number((double)value);
            if (value is Enum) return value.ToString();
            return value;
        }
        void RequirePattern(UiaElement e, string name) {
            int pattern;
            if (!NativeReadSchema.RequiredPatterns.TryGetValue(name, out pattern)) return;
            if (!Patterns(e).Any(p => p.Id == pattern)) throw new NativeFieldUnavailable("unsupported", "UIA does not expose the required pattern");
        }
        internal static Dictionary<string, object> Status(Exception error) {
            var failure = error as Failure;
            if (failure != null && (failure.Code == "cancelled" || failure.Code == "deadline_exceeded")) throw failure;
            var unavailable = error as NativeFieldUnavailable;
            return Wire.Obj("state", unavailable == null ? "error" : unavailable.State, "detail", NativeReadStore.Text(
                unavailable == null ? error.GetType().Name + " (0x" + error.HResult.ToString("X8") + "): " + error.Message : error.Message));
        }
        void Related(UiaElement item, NativeNodeRecord source, List<string> targets, ref string state, ref string detail) {
            try {
                if (item == null) throw new NativeFieldUnavailable("error", "UIA relation contains an unavailable object");
                targets.Add(store.KeepRelated(item, source, request));
            } catch (Exception error) {
                Status(error); Check(); state = "partial";
                detail = "A relation member could not be retained; other members are preserved: " + NativeReadStore.Text(error.Message);
            }
        }
        object Actions(UiaElement e) {
            var items = new List<object>();
            try {
                Check(); var supported = new HashSet<int>(Patterns(e).Select(p => p.Id)); request.Check();
                foreach (string name in NativeUiaActions.Names(supported)) items.Add(Wire.Obj("name", name, "description", name));
                // This is the no-argument action catalogue, not a claim that
                // parameterized Value/Scroll/Transform methods do not exist.
                return Wire.Obj("state", "complete", "items", items, "total", items.Count);
            } catch (Exception error) { Status(error); return Wire.Obj("state", "unavailable", "items", items, "detail", NativeReadStore.Text(error.Message)); }
        }
        Dictionary<string, object> Edge(NativeNodeRecord node, string property, int limit, string cursor = null) {
            Trace("relation " + node.Id + " " + property);
            Page page = null;
            if (cursor != null) {
                if (!pages.TryGetValue(cursor, out page) || page.Surface != node.Surface || page.Node != node.Id || page.Property != property)
                    throw new Failure("snapshot_expired", "native relation continuation expired or belongs to another scope");
            }
            var targets = new List<string>(); string state = "complete", detail = null;
            try {
                Check();
                if (property == "RawViewChildren") {
                    var seen = new HashSet<string>();
                    foreach (var child in cache.Children(node.Element)) {
                        Check();
                        if (targets.Count >= 4096) { state = "partial"; detail = "Native child enumeration limited to 4096 objects; no complete continuation snapshot."; break; }
                        int before = targets.Count;
                        Related(child, node, targets, ref state, ref detail);
                        if (targets.Count > before && !seen.Add(targets[targets.Count - 1])) {
                            targets.RemoveAt(targets.Count - 1); state = "partial"; detail = "Native child enumeration repeated an object."; break;
                        }
                    }
                } else if (property == "Text.Children") {
                    var children = TextReader(node).Children();
                    if (children == null) throw new NativeFieldUnavailable("absent", "UIA returned no embedded-object array");
                    foreach (var child in children) {
                        Check();
                        if (targets.Count >= 4096) { state = "partial"; detail = "Embedded-object limit reached."; break; }
                        Related(child, node, targets, ref state, ref detail);
                    }
                } else if (property == "ItemContainer.Items") {
                    Check(); object raw;
                    if (!node.Element.TryGetCurrentPattern(ItemContainerPattern.Pattern, out raw)) throw new NativeFieldUnavailable("unsupported");
                    var pattern = (UIA.IUIAutomationItemContainerPattern)raw; UiaElement previous = null;
                    var seen = new HashSet<string>();
                    while (true) {
                        Check(); var item = UiaClient.Wrap(pattern.FindItemByProperty(previous == null ? null : previous.Native, 0, null)); Check();
                        if (item == null) break;
                        if (targets.Count >= 4096) { state = "partial"; detail = "Logical item enumeration limited to 4096 items; remaining count unknown. Read the visible subtree after an explicit scroll to explore further."; break; }
                        string id = store.KeepRelated(item, node, request);
                        if (!seen.Add(id)) { state = "partial"; detail = "ItemContainer repeated an object; enumeration stopped."; break; }
                        targets.Add(id); previous = item;
                    }
                } else if (property == "Grid.Cells") {
                    Check(); object raw;
                    if (!node.Element.TryGetCurrentPattern(GridPattern.Pattern, out raw)) throw new NativeFieldUnavailable("unsupported");
                    var pattern = (UIA.IUIAutomationGridPattern)raw; int rows = pattern.CurrentRowCount, columns = pattern.CurrentColumnCount; Check();
                    long count = (long)rows * columns;
                    if (rows < 0 || columns < 0) throw new NativeFieldUnavailable("error", "UIA Grid reported negative dimensions");
                    for (int i = 0; i < Math.Min(count, 4096); i++) {
                        Check(); var cell = UiaClient.Wrap(pattern.GetItem(i / columns, i % columns)); Check();
                        if (cell == null) throw new NativeFieldUnavailable("error", "Grid.GetItem returned no cell");
                        Related(cell, node, targets, ref state, ref detail);
                    }
                    int afterRows = pattern.CurrentRowCount, afterColumns = pattern.CurrentColumnCount; Check();
                    if (afterRows != rows || afterColumns != columns) {
                        state = "partial"; detail = "Grid dimensions changed during observation.";
                    } else if (count > 4096) {
                        state = "partial"; detail = "Grid logical coverage limited to its first 4096 positions; " + count + " positions declared. No scrolling or realization performed.";
                    }
                } else if (property == "RawViewParent" || property == "LabeledBy") {
                    UiaElement related = property == "RawViewParent" ? UiaClient.Parent(node.Element) : Get(node.Element, AutomationElement.LabeledByProperty) as UiaElement;
                    if (related != null) {
                        // Parent is an observed reverse edge, not a request to
                        // traverse ancestors or an input-authority check.
                        // Labels are followed, so retain their scope proof.
                        if (property == "RawViewParent") Related(related, node, targets, ref state, ref detail);
                        else {
                            if (!store.Within(related, node.Window, request)) throw new NativeFieldUnavailable("unsupported", "relation leaves the selected native window scope");
                            targets.Add(store.Keep(related, node.Window, request));
                        }
                    }
                } else if (NativeReadSchema.TableRelations.ContainsKey(property)) {
                    RequirePattern(node.Element, property);
                    object value = Get(node.Element, NativeReadSchema.TableRelations[property]);
                    var single = value as UiaElement;
                    var related = value as UiaElement[];
                    bool owner = property == "GridItem.ContainingGrid" || property == "SelectionItem.SelectionContainer";
                    if (owner) {
                        if (value != null && single == null) throw new NativeFieldUnavailable("error", "UIA owner relation has an invalid type");
                        related = single == null ? new UiaElement[0] : new[] { single };
                    } else if (related == null) throw new NativeFieldUnavailable("error", "UIA member relation has an invalid type");
                    foreach (var item in related) {
                        Check();
                        if (targets.Count >= 4096) { state = "partial"; detail = "Native member enumeration limited to 4096 objects."; break; }
                        // Preserve the provider's ordered membership, even if
                        // an object repeats. Node traversal has its own set.
                        Related(item, node, targets, ref state, ref detail);
                    }
                } else throw new NativeFieldUnavailable("unsupported", "unknown UIA relation");
            } catch (Exception error) {
                var status = Status(error); string statusName = (string)status["state"];
                state = targets.Count > 0 ? "partial" : statusName; detail = (string)status["detail"];
            }
            // A cursor remembers the observed prefix, not authority to return
            // old children as a fresh fact. Re-enumerate and require the same
            // prefix and count; an unread suffix may contain newer live data.
            if (page != null && (state != "complete" || targets.Count != page.Targets.Length ||
                    !targets.Take(page.Offset).SequenceEqual(page.Targets.Take(page.Offset))))
                throw new Failure("snapshot_expired", "native relation changed or could not be verified since the observed page");
            if (state == "complete") return PageEdge(node, property, targets.ToArray(), page == null ? 0 : page.Offset, limit);
            var result = Wire.Obj("source", node.Id, "property", property, "targets", targets.Take(limit).ToArray(), "certainty", targets.Count == 0 ? "unknown" : "confirmed", "state", state, "detail", detail ?? "Native relation incomplete");
            return result;
        }
        Dictionary<string, object> PageEdge(NativeNodeRecord node, string property, string[] targets, int offset, int limit) {
            var shown = targets.Skip(offset).Take(limit).ToArray();
            foreach (string id in shown) store.ReadNode(id, node.Surface, request);
            int end = offset + shown.Length;
            var result = Wire.Obj("source", node.Id, "property", property, "targets", shown, "certainty", "confirmed", "state", end == targets.Length && offset == 0 ? "complete" : "partial", "total", targets.Length);
            if (offset != 0) result["offset"] = offset;
            if (end < targets.Length) {
                string cursor = NativeReadStore.Token("relation");
                pages[cursor] = new Page { Surface = node.Surface, Node = node.Id, Property = property, Targets = targets, Offset = end }; pageOrder.Enqueue(cursor);
                while (pageOrder.Count > 64) pages.Remove(pageOrder.Dequeue());
                result["cursor"] = cursor;
            }
            return result;
        }
        sealed class NodeRead {
            internal NativeNodeRecord Node;
            internal int Depth;
            internal Dictionary<string, object> Values, Statuses, Result;
        }
        void ReadFields(NodeRead row, IEnumerable<string> fields, NativeCapturedText.Budget budget) {
            foreach (string field in fields) {
                try {
                    object value = Field(row.Node, field, budget.RequestLimit);
                    var sample = value as NativeCapturedText.Sample;
                    var text = sample == null ? value as string : sample.Text;
                    if (text != null) texts.Observe(field, text, sample == null || sample.Complete, budget, row.Values, row.Statuses);
                    else row.Values[field] = value;
                } catch (Exception error) { row.Statuses[field] = Status(error); }
            }
        }
        object Nodes(Dictionary<string, object> target, Dictionary<string, object> query) {
            double start = Wire.Now;
            var fields = Wire.Array(query["fields"]).Cast<string>().ToArray(); int maximum = (int)Wire.Num(query, "maxNodes", 1, 2000, true), depthLimit = (int)Wire.Num(query, "maxDepth", 0, 2000, true);
            var root = store.ReadNode((string)target["node"], (string)target["surface"], request);
            var rootPage = query.ContainsKey("relationPage") ? Wire.Object(query["relationPage"]) : query.ContainsKey("childrenPage") ? Wire.Object(query["childrenPage"]) : null;
            string pageProperty = rootPage != null && rootPage.ContainsKey("property") ? Wire.Str(rootPage, "property") : "RawViewChildren";
            var followed = query.ContainsKey("followRelations") ? Wire.Array(query["followRelations"]).Cast<string>().ToArray() : new string[0];
            var properties = Relations.Concat(followed).Distinct().ToArray();
            cache = new NativeReadCache(fields, Check, message => NativeReadStore.Note(observationNotes, message));
            if (rootPage != null) properties = new[] { pageProperty }.Concat(properties.Where(p => p != pageProperty)).ToArray();
            if (maximum > 16384 / properties.Length) {
                maximum = 16384 / properties.Length;
                NativeReadStore.Note(observationNotes, "Node budget reduced to keep every requested relation status inside the wire graph budget.");
            }
            var pending = new Queue<Tuple<string, int>>(); pending.Enqueue(Tuple.Create(root.Id, 0));
            var deferred = new Queue<NodeRead>();
            var ordinaryFields = fields.Where(f => !f.StartsWith("Text.", StringComparison.Ordinal)).ToArray();
            var textFields = fields.Except(ordinaryFields).ToArray();
            var ordinaryRelations = properties.Where(p => p != "Text.Children").ToArray();
            var textRelations = properties.Except(ordinaryRelations).ToArray();
            var textBudget = texts.BeginBatch();
            var seen = new HashSet<string>(); var referenced = new HashSet<string>(); var nodes = new List<object>(); var edges = new List<object>(); var notes = observationNotes; int edgeBudget = 8000, wireBytes = 0;
            Action<NodeRead, string[]> readEdges = (row, names) => {
                foreach (string property in names) {
                    Dictionary<string, object> edge;
                    if (edgeBudget == 0) edge = Wire.Obj("source", row.Node.Id, "property", property, "targets", new string[0], "certainty", "unknown", "state", "partial", "detail", "Fact graph relation budget exhausted");
                    else {
                        var page = row.Node.Id == root.Id && property == pageProperty ? rootPage : null;
                        edge = Edge(row.Node, property, Math.Min(page == null ? 128 : (int)Wire.Num(page, "limit", 1, 1999, true), edgeBudget), page == null ? null : page["cursor"] as string);
                    }
                    var children = Wire.Array(edge["targets"]).Cast<string>().ToArray(); edgeBudget -= children.Length; edges.Add(edge);
                    wireBytes += Encoding.UTF8.GetByteCount(Wire.Json(edge));
                    foreach (string child in children) {
                        referenced.Add(child);
                        bool follow = property == "RawViewChildren" || property == "LabeledBy" || followed.Contains(property)
                            || rootPage != null && row.Node.Id == root.Id && property == pageProperty;
                        if (follow && row.Depth < depthLimit) pending.Enqueue(Tuple.Create(child, row.Depth + 1));
                    }
                }
            };
            // Collect the navigable structure and cached scalar facts first.
            // TextRange is synchronous and can be expensive even for a hidden
            // diagnostic text node. It must not starve unrelated descendants.
            // Text.Children can add new objects; those re-enter the same raw
            // queue before the next text enrichment. No nodes are filtered.
            while (pending.Count > 0 && nodes.Count < maximum || deferred.Count > 0) {
                if (wireBytes >= 8 * 1024 * 1024 || Wire.Now >= until) break;
                if (pending.Count == 0 || nodes.Count >= maximum) {
                    var row = deferred.Dequeue();
                    int before = Encoding.UTF8.GetByteCount(Wire.Json(row.Result));
                    ReadFields(row, textFields, textBudget);
                    wireBytes += Encoding.UTF8.GetByteCount(Wire.Json(row.Result)) - before;
                    readEdges(row, textRelations); continue;
                }
                request.Check(); var item = pending.Dequeue(); if (seen.Contains(item.Item1)) continue;
                NativeNodeRecord node;
                try { node = store.ReadNode(item.Item1, root.Surface, request); }
                catch (Exception error) {
                    if (item.Item1 == root.Id) throw;
                    Status(error); NativeReadStore.Note(notes, "A related object became unavailable during collection; its reference remains unread."); continue;
                }
                seen.Add(item.Item1);
                var values = new Dictionary<string, object>(); var statuses = new Dictionary<string, object>();
                var rowRead = new NodeRead { Node = node, Depth = item.Item2, Values = values, Statuses = statuses };
                ReadFields(rowRead, ordinaryFields, textBudget);
                rowRead.Result = Wire.Obj("node", node.Id, "requestedFields", fields, "values", values, "fieldStatus", statuses, "actions", Actions(node.Element));
                nodes.Add(rowRead.Result); wireBytes += Encoding.UTF8.GetByteCount(Wire.Json(rowRead.Result));
                readEdges(rowRead, ordinaryRelations);
                if (textFields.Length != 0 || textRelations.Length != 0) deferred.Enqueue(rowRead);
            }
            if (wireBytes >= 8 * 1024 * 1024 || Wire.Now >= until) {
                string state = Wire.Now >= until ? "timeout" : "partial";
                string detail = state == "timeout" ? "Bounded UIA collection time exhausted; remaining fields and relations are unread."
                    : "Native collection wire budget exhausted; remaining fields and relations are unread.";
                NativeReadStore.Note(notes, detail);
                foreach (var row in deferred) {
                    foreach (string field in textFields) row.Statuses[field] = Wire.Obj("state", state == "partial" ? "error" : state, "detail", detail);
                    foreach (string property in textRelations) edges.Add(Wire.Obj("source", row.Node.Id, "property", property,
                        "targets", new string[0], "certainty", "unknown", "state", "partial", "detail", detail));
                }
            }
            if (pending.Count > 0 || referenced.Except(seen).Any()) NativeReadStore.Note(notes, "Some related nodes remain unread due to collection scope, depth or node budget.");
            return Wire.Obj("kind", "nodes", "session", session, "surface", root.Surface, "platform", "windows", "observed", Stamp(start), "roots", new[] { root.Id }, "nodes", nodes,
                "requestedRelations", properties, "relations", edges, "unread", referenced.Except(seen).ToArray(), "stopReasons", notes);
        }
        object Directory(Dictionary<string, object> query) {
            double start = Wire.Now; var notes = new List<string>();
            bool accessibility = !NeedsRestart && (!query.ContainsKey("includeAccessibility") || (bool)query["includeAccessibility"]);
            var all = store.Catalog(request, notes, accessibility);
            string kind = Wire.Str(query, "kind");
            if (kind == "applications") {
                return Wire.Obj("kind", kind, "session", session, "platform", "windows", "observed", Stamp(start), "stopReasons", notes,
                    "scope", "runningWindows",
                    "applications", all.GroupBy(x => x.Application).Select(g => Wire.Obj("id", g.Key, "displayName", NativeReadStore.Text(g.First().Native.Name), "isRunning", true, "path", null, "identifier", null,
                        "windows", g.Select(w => Wire.Obj("selector", w.Native.Selector, "window", w.Native.Ref)).ToArray())).ToArray());
            }
            string application = Wire.Str(query, "application"); var selected = new HashSet<IntPtr>(all.Where(x => x.Application == application).Select(x => x.Native.Handle));
            if (selected.Count == 0) throw new Failure("stale_target", "the observed running application no longer has an available window");
            for (int n = 0; n < all.Count; n++) {
                var children = all.Where(x => !selected.Contains(x.Native.Handle) && selected.Contains(Native.GetWindow(x.Native.Handle, 4))).ToArray();
                if (children.Length == 0) break; foreach (var child in children) selected.Add(child.Native.Handle);
            }
            var entries = all.Where(x => selected.Contains(x.Native.Handle)).ToArray();
            var roots = accessibility && !NeedsRestart ? rootProbe.Read(entries, request) : null;
            if (NeedsRestart) {
                NativeReadStore.Note(notes, "UIA window enrichment timed out; Win32 window identities are preserved. The blocked read worker is isolated and replaced; UIA roots and modality are unknown.");
            }
            Trace("directory surfaces collected");
            var rows = new List<object>();
            foreach (var entry in entries) {
                request.Check();
                // Enumeration and enrichment are not atomic. A tooltip or
                // another window may close without ending the working window.
                // Its stale identity must not invalidate every other row.
                try { entry.Native.Validate(); }
                catch (Failure error) {
                    if (error.Code != "stale_target") throw;
                    NativeReadStore.Note(notes, "A window closed or was replaced during directory collection; its entry was omitted.");
                    continue;
                }
                string node = accessibility || NeedsRestart ? null : entry.RootNode;
                NativeRootProbe.Root root = null;
                if (roots != null) roots.TryGetValue(entry.Native.Handle, out root);
                if (root != null) {
                    if (root.Error != null) NativeReadStore.Note(notes, "Window UIA unavailable: " + root.Error);
                    try { Check(); if (root.Element != null) node = store.Keep(root.Element, entry, request); }
                    catch (Exception error) { Status(error); NativeReadStore.Note(notes, "Window UIA unavailable: " + NativeReadStore.Text(error.Message)); }
                }
                if (accessibility) entry.RootNode = node;
                IntPtr owner = Native.GetWindow(entry.Native.Handle, 4); var parent = all.FirstOrDefault(x => x.Native.Handle == owner);
                object modal = NeedsRestart ? null : accessibility ? (root == null ? null : root.Modal) : entry.ObservedModal;
                string title = Native.Title(entry.Native.Handle);
                if (accessibility && modal is bool) entry.ObservedModal = (bool)modal;
                if (title.Length >= 4095) NativeReadStore.Note(notes, "A native window title reached the 4096-character Win32 buffer and may be truncated.");
                rows.Add(Wire.Obj("window", entry.Native.Ref, "instance", entry.Native.Selector, "surface", entry.Native.Scope, "node", node, "title", NativeReadStore.Text(title),
                    "kind", "window", "parent", parent == null ? null : parent.Native.Ref, "parentState", parent != null ? "known" : owner == IntPtr.Zero ? "none" : "unknown", "modal", modal,
                    "nativeWindow", true, "accessibility", node != null));
            }
            return Wire.Obj("kind", kind, "session", session, "platform", "windows", "observed", Stamp(start), "stopReasons", notes, "application", application,
                "provider", Wire.Obj("status", NeedsRestart ? "unavailable" : !accessibility ? "notRequested" : rows.Cast<Dictionary<string, object>>().Any(x => (bool)x["accessibility"]) ? "available" : "unavailable", "attribute", null, "writeStatus", null), "surfaces", rows);
        }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            request = r; until = Math.Min(r.Deadline, Wire.Now + 2500);
            observationNotes = new List<string>(); textReaders.Clear();
            cache = new NativeReadCache(new string[0], Check, message => NativeReadStore.Note(observationNotes, message));
            string kind = Wire.Str(query, "kind");
            if (kind == "applications" || kind == "surfaces") return Directory(query);
            if (kind == "nodes") return Nodes(target, query);
            if (kind == "capturedText") return Wire.Obj("kind", kind, "session", session, "textRef", Wire.Str(query, "textRef"),
                "page", texts.Page(Wire.Str(query, "textRef"), (long)Wire.Num(query, "offset", 0, 9007199254740991, true),
                    (int)Wire.Num(query, "length", 2, NativeCapturedText.PreviewUnits, true)));
            double start = Wire.Now; var node = store.ReadNode((string)target["node"], (string)target["surface"], request);
            var edge = Edge(node, Wire.Str(query, "property"), (int)Wire.Num(query, "limit", 1, 2000, true), query["cursor"] as string);
            return Wire.Obj("kind", "relation", "session", session, "surface", node.Surface, "platform", "windows", "observed", Stamp(start), "relation", edge);
        }
    }
}
