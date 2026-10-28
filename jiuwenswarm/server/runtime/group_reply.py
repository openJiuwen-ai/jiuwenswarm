"""Associate final task results explicitly; ordinary messages stand on their own."""


def correlate_public_reply(group_id: str, message: dict) -> dict:
    from jiuwenswarm.server.runtime.group_execution import GroupExecutions

    if message["sender"] == "user":
        return {}
    client_id = message.get("client_message_id", "")
    prefix = "execution-result-"
    if client_id.startswith(prefix):
        execution_id = client_id[len(prefix):]
        record = GroupExecutions(group_id).read().get(execution_id)
        is_card = record is not None and record.get("presentation", "card") == "card"
        if is_card and record["member_name"] == message["sender"]:
            return {"execution_id": execution_id, "reply_id": record["reply_id"]}
    # Never guess the source invocation from an older unfinished reply.
    return {"execution_id": None, "reply_id": message["message_id"]}
