from openjiuwen.symphony import CapabilityDescriptor, CapabilityFingerprint

from jiuwenswarm.symphony.graph_state import (
    GraphState,
    GraphStateBuilder,
    GraphStateEntry,
)


def _descriptor(capability_id: str, content_hash: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id,
        capability_type="skill",
        name=capability_id.title(),
        content_hash=content_hash,
        metadata={"entrypoint": f"nested/{capability_id}/SKILL.md"},
    )


def _fingerprint(
    capability_id: str,
    *,
    static_data: dict | None = None,
) -> CapabilityFingerprint:
    return CapabilityFingerprint(
        capability_id=capability_id,
        capability_type="skill",
        name=capability_id.title(),
        content_hash=f"hash-{capability_id}",
        static_data=static_data or {},
    )


def test_graph_state_maps_core_entrypoint_and_complete_content_hash():
    descriptor = _descriptor("writer", "complete-assets-hash")
    builder = GraphStateBuilder()

    hashes = builder.capability_hashes([descriptor])
    state = builder.next_state(
        capabilities=[descriptor],
        current_hashes=hashes,
        fingerprints_by_id={"writer": _fingerprint("writer")},
        old_state=GraphState(),
        removed_paths=set(),
    )

    assert hashes == {"nested/writer": "complete-assets-hash"}
    assert state.schema_version == "Symphony-graph-state-v2"
    assert state.skills["nested/writer"].content_hash == "complete-assets-hash"


def test_graph_state_reads_legacy_skill_md_hash_for_one_time_migration():
    state = GraphState.from_dict(
        {
            "schema_version": "Symphony-graph-state-v1",
            "skills": {
                "writer": {
                    "skill_id": "writer",
                    "relative_path": "writer",
                    "skill_md_sha256": "legacy-hash",
                    "fingerprint_hash": "fingerprint-hash",
                }
            },
        }
    )

    assert state.active_entries()["writer"].content_hash == "legacy-hash"


def test_graph_identity_hash_excludes_non_graph_quality_extensions():
    first = _fingerprint("weather", static_data={"documentation": "first"})
    second = _fingerprint("weather", static_data={"documentation": "second"})

    assert first.graph_identity_dict() == second.graph_identity_dict()
    assert GraphStateBuilder.fingerprint_hash(
        first
    ) == GraphStateBuilder.fingerprint_hash(second)


def test_removed_graph_state_entry_preserves_previous_identity():
    old = GraphState(
        skills={
            "retired": GraphStateEntry(
                skill_id="retired",
                relative_path="retired",
                content_hash="old-content",
                fingerprint_hash="old-fingerprint",
            )
        }
    )

    state = GraphStateBuilder().next_state(
        capabilities=[],
        current_hashes={},
        fingerprints_by_id={},
        old_state=old,
        removed_paths={"retired"},
    )

    assert state.skills["retired"].status == "removed"
    assert state.skills["retired"].content_hash == "old-content"
