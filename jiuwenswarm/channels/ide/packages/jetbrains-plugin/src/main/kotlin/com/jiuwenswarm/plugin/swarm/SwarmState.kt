package com.jiuwenswarm.plugin.swarm

data class LaneFeedEntry(
    val text: String,
    var at: Long,
)

data class AgentLane(
    val memberName: String,
    var displayName: String,
    var role: String,                         // "LEADER" | "TEAMMATE" | "WORKER"
    var status: String = "READY",             // "READY" | "BUSY" | "PAUSED" | "SHUTDOWN"
    var executionStatus: String = "IDLE",     // "IDLE" | "RUNNING" | "COMPLETING"
    var currentTaskId: String? = null,
    var currentTaskTitle: String? = null,
    var currentActivity: String? = null,      // e.g. "writing · auth.service.ts"
    var lastToolName: String? = null,
    var lastActivePath: String? = null,       // full file path — used for jump-to-file on lane click
    var lastActiveAt: Long = System.currentTimeMillis(),
    var startedAt: Long? = null,              // when this lane became active (spawn / first tool)
    var activityFeed: MutableList<LaneFeedEntry> = mutableListOf(),  // recent activity, chronological, capped
    var messageCount: Int = 0,
    var tasksDone: Int = 0,
)

data class TeamTask(
    val taskId: String,
    var title: String,
    var status: String,   // "pending" | "in_progress" | "completed" | "cancelled"
    var assignee: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

/** A single inter-agent message (p2p or broadcast). */
data class TeamMessage(
    val from: String,
    val to: String?,      // null = broadcast
    val content: String,
    val timestamp: Long,
)

data class SwarmSnapshot(
    val sessionId: String,
    val teamName: String,
    val lanes: List<AgentLane>,
    val tasks: List<TeamTask>,
    val messages: List<TeamMessage>,   // last 50 inter-agent messages, chronological
    val lastEventAt: Long,
)
