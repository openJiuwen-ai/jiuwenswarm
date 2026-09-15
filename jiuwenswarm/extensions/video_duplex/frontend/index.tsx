import { VideoLivePanel } from "./VideoLivePanel";
import { VideoDuplexSettings } from "./VideoDuplexSettings";
import { TaskFullDuplexAction } from "./TaskFullDuplexAction";
import { TaskFullDuplexRuntime } from "./TaskFullDuplexRuntime";

export const applicationPluginId = "video-duplex";
export const applicationPluginSettings = VideoDuplexSettings;
export const applicationPluginTaskInputAction = TaskFullDuplexAction;
export const applicationPluginTaskRuntime = TaskFullDuplexRuntime;
export default VideoLivePanel;
