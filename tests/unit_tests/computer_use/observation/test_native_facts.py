import copy
import re
import unittest

from computer_use.observation.native_facts import DisplayReferences, normalize_facts
from computer_use.protocol.facts import decode_facts, decode_relation_page


def node(ref, **values):
    return dict(node=ref, requestedFields=list(values), values=values, fieldStatus={}, actions=dict(state="complete", items=[]))


def edge(source, *targets, property="AXChildren", **changes):
    return {**dict(source=source, property=property, targets=list(targets), certainty="confirmed", state="complete"), **changes}


def batch(nodes, relations=(), roots=None, **changes):
    relations = list(relations)
    properties = list(dict.fromkeys(relation["property"] for relation in relations))
    present = {(relation["source"], relation["property"]) for relation in relations}
    relations += [dict(source=node["node"], property=prop, targets=[], certainty="confirmed", state="absent")
                  for node in nodes for prop in properties if (node["node"], prop) not in present]
    return dict(kind="nodes", session="session", surface="surface", platform="macos",
                observed=dict(id="observation", startedAt=1, completedAt=2), roots=roots or [nodes[0]["node"]],
                nodes=nodes, requestedRelations=properties, relations=relations, unread=[], stopReasons=[], **changes)


class NativeFactsTests(unittest.TestCase):
    def test_editing_query_facts_keep_false_and_unknown_distinct(self):
        source = node('input', AXRole='AXTextField', AXEditable=True,
                      **{'settable.AXFocused': False, 'settable.AXValue': True})
        source['requestedFields'].append('settable.AXSelectedText')
        source['fieldStatus']['settable.AXSelectedText'] = dict(state='timeout')
        wire = batch([source])
        before = copy.deepcopy(wire)
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        self.assertEqual(snapshot.native, before)
        self.assertEqual(wire, before)
        rendered = snapshot.render()['text']
        self.assertIn('settable.AXSelectedText: timeout', rendered)
        self.assertNotIn('focusable', rendered, 'Settable declarations must not become receiver confirmation')

    def test_false_zero_empty_unsupported_and_not_requested_remain_distinct(self):
        source = node("control", AXRole="AXCheckBox", AXTitle="", AXValue=0, AXEnabled=False)
        source["requestedFields"].append("AXHelp")
        source["fieldStatus"]["AXHelp"] = dict(state="unsupported")
        wire = batch([source])
        before = copy.deepcopy(wire)
        semantic = normalize_facts(wire, DisplayReferences("session"))
        self.assertEqual(wire, before)
        self.assertFalse(semantic.roots[0]["enabled"])
        self.assertEqual(semantic.roots[0]["value"], "0")
        self.assertEqual(semantic.roots[0]["name"], "")
        self.assertNotIn("description", semantic.roots[0])
        output = semantic.render()
        self.assertIn("disabled", output["text"])
        self.assertIn("AXHelp: unsupported", output["text"])
        self.assertEqual(semantic.native, before)

    def test_shared_child_cycle_and_unread_edges_are_not_silently_removed(self):
        wire = batch([node("window", AXRole="AXWindow"), node("a", AXRole="AXGroup"), node("b", AXRole="AXGroup"),
                      node("button", AXRole="AXButton", AXTitle="Shared")],
                     [edge("window", "a", "b"), edge("a", "button"), edge("b", "button"), edge("button", "window", "outside")])
        wire["unread"] = ["outside"]
        result = normalize_facts(wire, DisplayReferences("session")).render()
        self.assertIn("(shared)", result["text"])
        self.assertIn("(cycle)", result["text"])
        self.assertIn("(unread)", result["text"])
        self.assertEqual(set(result["refs"].values()), {"window", "a", "b", "button", "outside"})
        self.assertEqual(result["text"].count("Shared"), 1)

    def test_declared_label_action_retains_its_own_target(self):
        label = node("label", AXRole="AXStaticText", AXValue="Wi-Fi")
        label["actions"]["items"] = [dict(name="AXShowMenu", description="Show menu")]
        raw = batch([node("row", AXRole="AXRow", AXSelected=True), node("layout", AXRole="AXCell"), label],
                    [edge("row", "layout"), edge("layout", "label")])
        semantic = normalize_facts(raw, DisplayReferences("session"))
        output = semantic.render()
        ref = next(ref for ref, target in output["refs"].items() if target == "label")
        self.assertRegex(output["text"], re.escape(ref) + r" .*Show menu")
        self.assertNotIn("clickable", output["text"], "Role alone is not permission to click")
        self.assertEqual(semantic.roots[0]["actions"], [])

    def test_unknown_fields_and_roles_and_unavailable_actions_are_visible(self):
        source = node("custom", AXRole="custom role / outside known vocabulary", VendorState={"status": False, "index": 0})
        source["actions"] = dict(state="unavailable", items=[], detail="provider query failed")
        rendered = normalize_facts(batch([source]), DisplayReferences("session")).render()
        self.assertIn("custom role / outside known vocabulary", rendered["text"])
        self.assertIn('VendorState: {"status":false,"index":0}', rendered["text"])
        self.assertIn("provider query failed", rendered["text"])

    def test_text_preview_keeps_its_immutable_source_and_explicit_limit(self):
        source = node("text", AXRole="AXTextField", AXValue="preview")
        source["fieldStatus"]["AXValue"] = dict(state="truncated", textRef="captured-text", totalUnits=9000)
        snapshot = normalize_facts(batch([source]), DisplayReferences("session"))
        self.assertEqual(snapshot.text_refs[("0", "AXValue")]["textRef"], "captured-text")
        self.assertIn("captured text available", snapshot.render()["text"])
        source["fieldStatus"]["AXValue"] = dict(state="truncated", detail="native text capture unavailable")
        output = normalize_facts(batch([source]), DisplayReferences("session")).render()
        self.assertIn("native text capture unavailable", output["text"])
        self.assertEqual(output["textRefs"], {})

    def test_conflicting_edges_are_annotations_not_ownership(self):
        relation = edge("a", "b")
        relation["certainty"] = "conflicting"
        wire = batch([node("a", AXRole="AXWindow"), node("b", AXRole="AXButton", AXTitle="Other")], [relation])
        snapshot = normalize_facts(wire, DisplayReferences("session"))
        self.assertEqual(len(snapshot.roots), 2)
        self.assertEqual(snapshot.roots[0]["children"], [])
        self.assertIn("conflicting", snapshot.render()["text"])

    def test_platform_mapping_is_shared_and_numbers_are_stable(self):
        for platform, role, title in (("macos", "AXButton", "AXTitle"), ("windows", "ControlType.Button", "Name"), ("linux", "push button", "name")):
            source = node("button", **{dict(macos="AXRole", windows="ControlType", linux="role")[platform]: role, title: "Open"})
            raw = batch([source]); raw["platform"] = platform
            references = DisplayReferences("session")
            first = normalize_facts(raw, references)
            self.assertEqual(first.roots[0]["role"], "button")
            self.assertEqual(first.roots[0]["name"], "Open")
            self.assertEqual(normalize_facts(raw, references).refs, first.refs)
            with self.assertRaises(ValueError):
                normalize_facts(raw, DisplayReferences("new-session"))

    def test_bounded_reference_history_never_recycles_a_display_number(self):
        references = DisplayReferences("session", limit=2)
        self.assertEqual(references.reference("a"), "0")
        self.assertEqual(references.reference("b"), "1")
        self.assertEqual(references.reference("a"), "0")
        self.assertEqual(references.reference("c"), "2")
        self.assertEqual(references.reference("b"), "3")
        self.assertEqual(len(references._refs), 2)

    def test_broken_protocol_is_not_confused_with_an_empty_app(self):
        good = batch([node("field", AXRole="AXTextField")])
        bad = []
        case = copy.deepcopy(good); case["nodes"][0]["requestedFields"].append("AXValue"); bad.append(case)
        case = copy.deepcopy(good); case["nodes"][0]["fieldStatus"]["AXRole"] = dict(state="error"); bad.append(case)
        case = copy.deepcopy(good); case["nodes"].append(copy.deepcopy(case["nodes"][0])); bad.append(case)
        case = copy.deepcopy(good); case["relations"] = [edge("field", "undeclared")]; bad.append(case)
        case = copy.deepcopy(good); case["observed"]["completedAt"] = 0; bad.append(case)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_facts(value)

    def test_unread_relation_is_not_represented_as_absent(self):
        source = batch([node("root", AXRole="AXWindow")], [edge("root", property="AXParent")])
        source["relations"][0].update(state="timeout", certainty="unknown", detail="collection deadline")
        from .test_relation_accounting import relation_records
        rendered = normalize_facts(source, DisplayReferences("session")).render()
        relation = relation_records(rendered)[('0', 'AXParent')]
        self.assertEqual((relation['state'], relation['certainty']), ('timeout', 'unknown'))
        source["relations"] = []
        with self.assertRaisesRegex(ValueError, "every requested relation"):
            decode_facts(source)

    def test_action_catalogue_and_description_coverage_are_separate(self):
        source = node("button", AXRole="AXButton")
        source["actions"] = dict(state="complete", total=1, items=[dict(name="AXPress", descriptionStatus=dict(state="timeout"))])
        output = normalize_facts(batch([source]), DisplayReferences("session")).render()["text"]
        self.assertIn("AXPress", output)
        self.assertIn('"descriptionStatus":{"state":"timeout"}', output)
        self.assertEqual(output.count('"descriptionStatus"'), 1, 'Action status should have one complete catalogue destination')
        source["actions"].update(state="partial", total=2, detail="one malformed action name")
        self.assertIn("actions: partial (1/2)", normalize_facts(batch([source]), DisplayReferences("session")).render()["text"])
        source["actions"]["state"] = "complete"
        with self.assertRaisesRegex(ValueError, "action coverage"):
            decode_facts(batch([source]))

    def test_duplicate_relations_and_omitted_descriptions_are_rejected(self):
        wire = batch([node("root", AXRole="AXWindow")], [edge("root")])
        wire["relations"].append(copy.deepcopy(wire["relations"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate native relation"):
            decode_facts(wire)
        wire = batch([node("root", AXRole="AXButton")])
        wire["nodes"][0]["actions"]["items"] = [dict(name="AXPress")]
        with self.assertRaisesRegex(ValueError, "description needs"):
            decode_facts(wire)

    def test_only_exact_containment_inverses_can_be_omitted_from_annotations(self):
        wire = batch([node("window", AXRole="AXWindow"), node("child", AXRole="AXButton", AXTitle="Open"),
                      node("other", AXRole="AXWindow")],
                     [edge("window", "child"), edge("child", "window", property="AXParent"),
                      edge("child", "other", property="AXWindow")])
        snapshot = normalize_facts(wire, DisplayReferences("session"))
        rendered = snapshot.render()
        from .test_relation_accounting import relation_records
        explicit = relation_records(rendered)
        self.assertNotIn(('1', 'AXParent'), explicit)
        self.assertEqual(explicit[('1', 'AXWindow')]['targets'], ['2'])
        self.assertEqual(snapshot.native, wire)
        source = next(s for s in rendered["relationSources"] if s["source"] == "child" and s["property"] == "AXParent")
        self.assertEqual(source["representation"], "containment")
        self.assertEqual(len(rendered["relationSources"]), len(wire["relations"]))

    def test_repeated_absence_summary_preserves_reference_sets_compactly(self):
        nodes = [node(str(i), AXRole="AXStaticText", AXValue=str(i)) for i in range(25)]
        for item in nodes:
            item["requestedFields"].append("AXHelp")
            item["fieldStatus"]["AXHelp"] = dict(state="unsupported")
        snapshot = normalize_facts(batch(nodes), DisplayReferences("session"))
        self.assertIn("0–24 AXHelp: unsupported", snapshot.render()["text"])
        self.assertEqual(len(snapshot.native["nodes"]), 25)

    def test_relation_suffix_ranges_are_preserved_not_rendered_as_full_children(self):
        relation = edge('root', 'child', state='partial', offset=2, total=3)
        wire = batch([node('root', AXRole='AXGroup'), node('child', AXRole='AXButton', AXTitle='Third')], [relation])
        snapshot = normalize_facts(wire, DisplayReferences('session'))
        from .test_relation_accounting import relation_records
        explicit = relation_records(snapshot.render())[('0', 'AXChildren')]
        self.assertEqual((explicit['state'], explicit['offset'], explicit['total'], explicit['targets']),
                         ('partial', 2, 3, ['1']))
        self.assertEqual(snapshot.native['relations'][0]['offset'], 2)

    def test_relation_page_schema_rejects_false_completeness_and_invalid_ranges(self):
        good = dict(kind='relation', session='session', surface='surface', platform='macos',
                    observed=dict(id='page', startedAt=1, completedAt=2),
                    relation=edge('root', 'child', state='partial', offset=1, total=4, cursor='continue'))
        self.assertEqual(decode_relation_page(good), good)
        for update in (dict(offset=4), dict(state='complete'), dict(state='absent'), dict(state='error'), dict(total=2), dict(offset=-1)):
            invalid = copy.deepcopy(good)
            invalid['relation'].update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                decode_relation_page(invalid)
        missing = copy.deepcopy(good)
        missing['relation'].pop('total')
        with self.assertRaisesRegex(ValueError, 'observed total'):
            decode_relation_page(missing)


if __name__ == "__main__":
    unittest.main()
