# HyperAgent media

This package prepares channel images and limits vision-tool reads to the current message or session. It does not own task scheduling, channel message delivery, or Issue repair.

| Module | Responsibility |
| --- | --- |
| `execution_media.py` | Channel adapter protocols, execution-scoped file access, and cancellable media preparation |
| `vision_runtime.py` | Unified vision configuration resolution and checked VQA/OCR tool invocation |
| `feishu_inbound_media.py` | Bound-bot authentication, source validation, bounded image download, and cleanup |

`execution_driver.py` owns the lifecycle and passes the scope through invocation extras. `bash_runtime.py` assembles the existing tools with that scope. `welink_inbound.py` remains a channel entry point because it accepts whole messages, not just media.

Channel adapters implement `InboundMedia`; the common execution scope must not depend on a channel API. Import the owning module directly; `__init__.py` does not eagerly register or re-export runtime objects. Media cache paths and persisted reference identifiers are unchanged by this package layout.
