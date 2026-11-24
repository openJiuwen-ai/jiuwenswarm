import { webRequest } from './webClient';

// 薄封装，照抄 connectorApi.ts 惯例：一个方法一个 webRequest，不额外包装错误处理。
// 后端 handler 见 jiuwenswarm/server/handlers/session.py 的
// handle_session_equipment_defaults_get/set 的薄封装，方法名/参数按
// session_equipment.defaults.* 命名，响应体即 handler payload。
//
// get 返回单一来源视图（2026-11-24 社区简化：预置层已删，仅剩 state.json
// is_default）：mcp=生效默认集（活标记，已按活性过滤）；runtime=state.json 里所有
// 被标记的（含断连）；unavailable=被标记但当前不可用的子集。
//
// set 是 PUT 语义：mcp 传"完整期望清单"（不是增量），后端在单锁内原子替换全部
// is_default 标记；每个名字必须已连接（connected/connecting），否则 BAD_REQUEST。
// set 的响应只回 mcp/runtime 两键，调用方需要完整视图时 merge 旧值或重新 get。

export interface EquipmentDefaultsView {
  mcp: string[];
  runtime: string[];
  unavailable?: string[];
}

export async function getEquipmentDefaults(): Promise<EquipmentDefaultsView> {
  return webRequest<EquipmentDefaultsView>('session_equipment.defaults.get');
}

export async function setEquipmentDefaults(mcp: string[]): Promise<EquipmentDefaultsView> {
  return webRequest<EquipmentDefaultsView>('session_equipment.defaults.set', { mcp });
}
