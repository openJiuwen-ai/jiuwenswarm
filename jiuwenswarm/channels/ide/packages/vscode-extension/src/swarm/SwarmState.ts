export interface LaneFeedEntry {
  text: string;
  at: number;
}

export interface AgentLane {
  memberName: string;
  displayName: string;
  role: string;            // "LEADER" | "TEAMMATE" | "WORKER"
  status: string;          // "READY" | "BUSY" | "PAUSED" | "SHUTDOWN"
  executionStatus: string; // "IDLE" | "RUNNING" | "COMPLETING"
  currentTaskId: string | null;
  currentTaskTitle: string | null;
  currentActivity: string | null; // e.g. "writing · auth.service.ts"
  lastToolName: string | null;
  lastActivePath: string | null;  // full file path — used for jump-to-file on lane click
  lastActiveAt: number;
  startedAt: number | null;       // when this lane became active (spawn / first tool)
  activityFeed: LaneFeedEntry[];  // recent activity, chronological, capped
  messageCount: number;
  tasksDone: number;
}

export interface TeamTask {
  taskId: string;
  title: string;
  status: string; // "pending" | "in_progress" | "completed" | "cancelled"
  assignee: string | null;
  createdAt: number;
}

/** A single inter-agent message (p2p or broadcast). */
export interface TeamMessage {
  from: string;
  to: string | null;   // null = broadcast
  content: string;
  timestamp: number;
}

export interface SwarmSnapshot {
  sessionId: string;
  teamName: string;
  lanes: AgentLane[];
  tasks: TeamTask[];
  messages: TeamMessage[];  // last 50 inter-agent messages, chronological
  lastEventAt: number;
}
