import { VideoLivePanel } from "./VideoLivePanel";
import { TaskFullDuplexAction } from "./TaskFullDuplexAction";
import { TaskFullDuplexRuntime } from "./TaskFullDuplexRuntime";

export const applicationPluginId = "video-duplex";
export const applicationPluginTaskInputAction = TaskFullDuplexAction;
export const applicationPluginTaskRuntime = TaskFullDuplexRuntime;
export default VideoLivePanel;
