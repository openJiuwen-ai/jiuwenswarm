package com.jiuwenswarm.plugin.swarm

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.intellij.openapi.diagnostic.logger
import java.io.File

private val LOG = logger<SwarmStateManager>()
private val gson = Gson()

/**
 * Maintains in-memory swarm state built from team event deltas that arrive
 * over the WebSocket as "team.event:{json}" prefixed chat.delta messages.
 *
 * All public methods are @Synchronized — onJiuwenMessage runs on a background
 * thread while postSnapshot reads may happen on any thread.
 */
class SwarmStateManager {

    private var sessionId: String = ""
    private var teamName: String = ""
    private val lanes = LinkedHashMap<String, AgentLane>()   // insertion order = join order
    private val tasks = LinkedHashMap<String, TeamTask>()
    private val messages = ArrayDeque<TeamMessage>()          // ring buffer, max 50
    @Volatile private var lastEventAt: Long = 0L

    // ──────────────────────────────────────────
    // Public API
    // ──────────────────────────────────────────

    /**
     * Parse the JSON string that follows "team.event:" and apply it to state.
     * Expected shape: { "event": { "type": "team.member.spawned", ... } }
     */
    @Synchronized
    fun applyTeamEvent(json: String) {
        runCatching {
            val root = gson.fromJson(json, JsonObject::class.java)
            val event = normalizeEvent(root.getAsJsonObject("event") ?: root)  // tolerate flat shape
            val type = event.get("type")?.asString ?: return
            lastEventAt = event.get("timestamp")?.asLong ?: System.currentTimeMillis()

            // Capture team name from the first event that carries it
            event.get("team_name")?.asString?.let { if (teamName.isEmpty()) teamName = it }

            when {
                type == "team.member.spawned"          -> onMemberSpawned(event)
                type == "team.member.status_changed"   -> onMemberStatusChanged(event)
                type == "team.member.execution_changed"-> onMemberExecutionChanged(event)
                type == "team.member.activity_changed" -> onMemberActivityChanged(event)
                type == "team.member.shutdown"         -> onMemberShutdown(event)
                type == "team.task.created"            -> onTaskCreated(event)
                type == "team.task.claimed"            -> onTaskClaimed(event)
                type == "team.task.started"            -> onTaskStarted(event)
                type == "team.task.completed"          -> onTaskCompleted(event)
                type == "team.task.cancelled"          -> onTaskCancelled(event)
                type == "team.task.status_snapshot"    -> onTaskStatusSnapshot(event)
                type.startsWith("team.message.")       -> onMessage(event)
                else -> LOG.debug("SWARM → unhandled event type: $type")
            }
        }.onFailure { LOG.warn("SWARM → failed to apply team event: ${it.message}") }
    }

    /**
     * Map the gateway's team-event field names to the shape this manager consumes.
     * The gateway sends member_id/name/mode/team_id; the lanes here use
     * member_name/display_name/role/team_name. Runs before dispatch so every
     * handler below reads one consistent vocabulary.
     */
    private fun normalizeEvent(raw: JsonObject): JsonObject {
        val e = raw.deepCopy()
        if (e.has("member_id") && !e.has("member_name")) {
            e.get("member_id")?.takeIf { it.isJsonPrimitive }?.asString?.let { e.addProperty("member_name", it) }
        }
        if (e.has("name") && !e.has("display_name")) {
            e.get("name")?.takeIf { it.isJsonPrimitive }?.asString?.let { e.addProperty("display_name", it) }
        }
        if (e.has("mode") && !e.has("role")) {
            val mode = e.get("mode")
            if (mode != null && mode.isJsonPrimitive) {
                e.addProperty("role", mode.asString.uppercase())
            } else if (mode != null) {
                e.add("role", mode)
            }
        }
        if (e.has("team_id") && !e.has("team_name")) {
            e.get("team_id")?.takeIf { it.isJsonPrimitive }?.asString?.let { e.addProperty("team_name", it) }
        }
        return e
    }

    /** Map a gateway status into the map's lane vocabulary (READY/BUSY/PAUSED/SHUTDOWN). */
    private fun normalizeLaneStatus(s: String?): String? {
        if (s.isNullOrEmpty()) return null
        return when (s.uppercase()) {
            "IDLE"      -> "READY"
            "RUNNING"   -> "RUNNING"
            "COMPLETED" -> "SHUTDOWN"
            else        -> s.uppercase()
        }
    }

    /**
     * Called from the chat.tool_call / chat.tool_update branches of onJiuwenMessage.
     * memberName may be null if the server does not include it in the payload.
     * args (the parsed tool arguments) lets the feed say what an action touches —
     * e.g. a file_path, a glob pattern, or the shell command.
     */
    @Synchronized
    fun applyToolCall(toolName: String, filePath: String?, memberName: String?, args: JsonObject? = null) {
        val name = memberName ?: return   // cannot attribute without a member name
        val lane = lanes.getOrPut(name) { stubLane(name) }
        lane.lastToolName = toolName
        val activity = formatActivity(toolName, filePath, args)
        lane.currentActivity = activity
        lane.lastActivePath = filePath
        lane.lastActiveAt = System.currentTimeMillis()
        lane.status = "BUSY"
        pushFeed(lane, activity, lane.lastActiveAt)
        lastEventAt = lane.lastActiveAt
    }

    /** Model call started — show "thinking…" on the lane instead of drifting into idle. */
    @Synchronized
    fun applyModelCallStart(memberName: String?) {
        val name = memberName ?: return
        val lane = lanes.getOrPut(name) { stubLane(name) }
        lane.status = "BUSY"
        lane.currentActivity = "thinking…"
        lane.lastActiveAt = System.currentTimeMillis()
        pushFeed(lane, "thinking…", lane.lastActiveAt)
        lastEventAt = lane.lastActiveAt
    }

    /** First streamed token for a call: flip the lane from "thinking…" to "generating…". */
    @Synchronized
    fun applyModelTokenStart(memberName: String?) {
        val name = memberName ?: return
        val lane = lanes[name] ?: return
        if (lane.currentActivity != "thinking…") return
        lane.currentActivity = "generating…"
        lane.lastActiveAt = System.currentTimeMillis()
    }

    @Synchronized
    fun applyModelCallEnd(memberName: String?) {
        val name = memberName ?: return
        val lane = lanes[name] ?: return
        if (lane.currentActivity == "thinking…" || lane.currentActivity == "generating…") {
            lane.currentActivity = null
        }
        lane.lastActiveAt = System.currentTimeMillis()
    }

    @Synchronized
    fun snapshot(): SwarmSnapshot {
        // Stable insertion order (join order) — lanes must not jump around as
        // statuses change; the webview renders status separately from position.
        val lanesList = lanes.values.toList()
        val sortedTasks = tasks.values.sortedWith(compareBy { it.createdAt })
        return SwarmSnapshot(
            sessionId = sessionId,
            teamName = teamName,
            lanes = lanesList,
            tasks = sortedTasks,
            messages = messages.toList(),
            lastEventAt = lastEventAt,
        )
    }

    @Synchronized
    fun reset(newSessionId: String) {
        sessionId = newSessionId
        teamName = ""
        lanes.clear()
        tasks.clear()
        messages.clear()
        lastEventAt = 0L
    }

    fun isEmpty(): Boolean = lanes.isEmpty()

    // ──────────────────────────────────────────
    // Event handlers
    // ──────────────────────────────────────────

    private fun onMemberSpawned(e: JsonObject) {
        val name = e.get("member_name")?.asString ?: return
        val status = normalizeLaneStatus(e.get("status")?.asString) ?: "READY"
        val prompt = e.get("prompt")?.asString
        val existing = lanes[name]
        if (existing != null) {
            // Update display info in case of restart
            existing.displayName = e.get("display_name")?.asString ?: existing.displayName
            existing.role = e.get("role")?.asString ?: existing.role
            existing.status = status
            if (!prompt.isNullOrEmpty()) {
                existing.currentTaskTitle = prompt
                existing.currentActivity = "starting"
            }
            existing.startedAt = existing.startedAt ?: lastEventAt
            pushFeed(existing, "spawned", lastEventAt)
            existing.lastActiveAt = lastEventAt
        } else {
            lanes[name] = AgentLane(
                memberName = name,
                displayName = e.get("display_name")?.asString ?: name,
                role = e.get("role")?.asString ?: "TEAMMATE",
                status = status,
                currentTaskTitle = prompt,
                currentActivity = if (prompt.isNullOrEmpty()) null else "starting",
                lastActiveAt = lastEventAt,
                startedAt = lastEventAt,
                activityFeed = mutableListOf(LaneFeedEntry(text = "spawned", at = lastEventAt)),
            )
        }
    }

    private fun onMemberStatusChanged(e: JsonObject) {
        val name = e.get("member_name")?.asString ?: return
        val lane = lanes.getOrPut(name) { stubLane(name) }
        val newStatus = e.get("new_status")?.takeIf { it.isJsonPrimitive }?.asString
            ?: e.get("status")?.takeIf { it.isJsonPrimitive }?.asString
        val status = normalizeLaneStatus(newStatus)
        status?.let { lane.status = it }
        val outcome = e.get("outcome")?.asString
        if (!outcome.isNullOrEmpty() && status == "SHUTDOWN") {
            lane.currentActivity = "done · $outcome"
        }
        if (status == "SHUTDOWN") pushFeed(lane, "finished", lastEventAt)
        else if (status == "PAUSED") pushFeed(lane, "paused", lastEventAt)
        lane.lastActiveAt = lastEventAt
    }

    private fun onMemberExecutionChanged(e: JsonObject) {
        val name = e.get("member_name")?.asString ?: return
        val lane = lanes.getOrPut(name) { stubLane(name) }
        val execution = e.get("new_status")?.takeIf { it.isJsonPrimitive }?.asString
            ?: e.get("execution_status")?.takeIf { it.isJsonPrimitive }?.asString
        if (!execution.isNullOrEmpty()) lane.executionStatus = execution.uppercase()
        lane.lastActiveAt = lastEventAt
    }

    /** Live mid-run activity (e.g. a swarmflow worker tool call) → lane activity. */
    private fun onMemberActivityChanged(e: JsonObject) {
        val name = e.get("member_name")?.asString ?: return
        val activity = e.get("activity")?.asString ?: return
        val lane = lanes.getOrPut(name) { stubLane(name) }
        lane.status = "BUSY"
        lane.currentActivity = activity
        pushFeed(lane, activity, lastEventAt)
        lane.lastActiveAt = lastEventAt
    }

    private fun onMemberShutdown(e: JsonObject) {
        val name = e.get("member_name")?.asString ?: return
        val lane = lanes[name] ?: return
        lane.status = "SHUTDOWN"
        lane.executionStatus = "IDLE"
        lane.currentActivity = null
        pushFeed(lane, "shutdown", lastEventAt)
        lane.lastActiveAt = lastEventAt
    }

    private fun onTaskCreated(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        tasks[id] = TeamTask(
            taskId = id,
            title = e.get("title")?.asString ?: id,
            status = "pending",
            createdAt = lastEventAt,
        )
    }

    private fun onTaskClaimed(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        val task = tasks.getOrPut(id) { TeamTask(id, e.get("title")?.asString ?: id, "pending") }
        // Gateway names the claimer member_id; normalizeEvent maps it to member_name.
        task.assignee = e.get("assignee")?.asString ?: e.get("member_name")?.asString
        // Server carries the authoritative status (e.g. "in_progress") — prefer it;
        // fall back to started semantics for older gateways that only mark claimed.
        task.status = e.get("status")?.asString ?: "in_progress"
        // Link task into the lane so the board reflects the current work.
        task.assignee?.let { memberName ->
            lanes[memberName]?.let { lane ->
                lane.currentTaskId = id
                lane.currentTaskTitle = task.title
                lane.lastActiveAt = lastEventAt
            }
        }
    }

    /** Convergence event: apply a task's authoritative status/assignee snapshot. */
    private fun onTaskStatusSnapshot(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        val status = e.get("status")?.asString
        val assignee = e.get("assignee")?.asString ?: e.get("member_name")?.asString
        val task = tasks.getOrPut(id) { TeamTask(id, e.get("title")?.asString ?: id, "pending") }
        if (!status.isNullOrEmpty()) task.status = status
        if (!assignee.isNullOrEmpty()) task.assignee = assignee
        task.assignee?.let { memberName ->
            lanes[memberName]?.let { lane ->
                if (task.status == "completed" && lane.currentTaskId == id) {
                    lane.currentTaskId = null
                    lane.currentTaskTitle = null
                    lane.tasksDone++
                    lane.lastActiveAt = lastEventAt
                }
            }
        }
    }

    private fun onTaskStarted(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        val task = tasks.getOrPut(id) { TeamTask(id, id, "pending") }
        task.status = "in_progress"
        task.assignee = e.get("assignee")?.asString ?: task.assignee
        // Link task into the lane
        task.assignee?.let { memberName ->
            lanes[memberName]?.let { lane ->
                lane.currentTaskId = id
                lane.currentTaskTitle = task.title
                lane.lastActiveAt = lastEventAt
            }
        }
    }

    private fun onTaskCompleted(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        val task = tasks[id] ?: return
        task.status = "completed"
        // Clear task from lane
        task.assignee?.let { memberName ->
            lanes[memberName]?.let { lane ->
                if (lane.currentTaskId == id) {
                    lane.currentTaskId = null
                    lane.currentTaskTitle = null
                    lane.tasksDone++
                    lane.lastActiveAt = lastEventAt
                }
            }
        }
    }

    private fun onTaskCancelled(e: JsonObject) {
        val id = e.get("task_id")?.asString ?: return
        val task = tasks[id] ?: return
        task.status = "cancelled"
        task.assignee?.let { memberName ->
            lanes[memberName]?.let { lane ->
                if (lane.currentTaskId == id) {
                    lane.currentTaskId = null
                    lane.currentTaskTitle = null
                    lane.lastActiveAt = lastEventAt
                }
            }
        }
    }

    private fun onMessage(e: JsonObject) {
        val from = e.get("from_member")?.asString ?: return
        val lane = lanes.getOrPut(from) { stubLane(from) }
        lane.messageCount++
        lane.lastActiveAt = lastEventAt
        // Capture message content for the inter-agent message log
        val content = e.get("content")?.asString ?: return
        val to = e.get("to_member")?.asString
        messages.addLast(TeamMessage(from = from, to = to, content = content, timestamp = lastEventAt))
        if (messages.size > 50) messages.removeFirst()
    }

    // ──────────────────────────────────────────
    // Helpers
    // ──────────────────────────────────────────

    private fun stubLane(memberName: String) = AgentLane(
        memberName = memberName,
        displayName = memberName,
        role = "TEAMMATE",
        lastActiveAt = lastEventAt,
        startedAt = lastEventAt.takeIf { it != 0L } ?: System.currentTimeMillis(),
    )

    /** Append one entry to a lane's activity feed, keeping it capped.
     *  A duplicate of the previous entry within 1s (the tool_call + tool_update
     *  pair for one call) is collapsed into the existing row instead of flooding
     *  the feed; genuinely separate calls still get their own rows. */
    private fun pushFeed(lane: AgentLane, text: String, at: Long) {
        val last = lane.activityFeed.lastOrNull()
        if (last != null && last.text == text && at - last.at < 1000) {
            last.at = at
            return
        }
        lane.activityFeed.add(LaneFeedEntry(text = text, at = at))
        while (lane.activityFeed.size > 10) lane.activityFeed.removeAt(0)
    }

    private fun formatActivity(toolName: String, filePath: String?, args: JsonObject?): String {
        val base = filePath?.let { File(it).name } ?: filePath
        val pattern = args?.get("pattern")?.takeIf { it.isJsonPrimitive }?.asString
        val command = args?.get("command")?.takeIf { it.isJsonPrimitive }?.asString
            ?: args?.get("cmd")?.takeIf { it.isJsonPrimitive }?.asString
            ?: args?.get("command_line")?.takeIf { it.isJsonPrimitive }?.asString
        val taskId = args?.get("task_id")?.takeIf { it.isJsonPrimitive }?.asString
        val to = args?.get("to")?.takeIf { it.isJsonPrimitive }?.asString
        return when (toolName) {
            "write_file", "create_file" -> if (base != null) "writing · $base" else "writing"
            "str_replace_editor", "edit_file" -> if (base != null) "editing · $base" else "editing"
            "read_file"                 -> if (base != null) "reading · $base" else "reading"
            "bash", "run_command", "powershell", "shell", "cmd"
                                        -> if (command != null) "running · ${command.take(40)}" else "running command"
            "list_files"                -> if (pattern != null) "listing · $pattern" else if (base != null) "listing · $base" else "listing"
            "glob", "grep", "search_files"
                                        -> if (pattern != null) "searching · $pattern" else if (base != null) "searching · $base" else "searching"
            "view_task"                 -> if (taskId != null) "viewing task · $taskId" else "viewing task"
            "claim_task"                -> if (taskId != null) "claiming task · $taskId" else "claiming task"
            "send_message"              -> if (to != null) "messaging · $to" else "messaging"
            else                        -> toolName
        }
    }
}
