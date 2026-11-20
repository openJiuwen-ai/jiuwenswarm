import { webRequest } from './webClient';

// 薄封装，照抄 connectorApi.ts 惯例：一个方法一个 webRequest，不额外包装错误处理。
// 后端 handler 见 jiuwenswarm/server/handlers/session.py 的
// handle_session_equipment_defaults_get/set（SDD-0030 阶段一已实现），方法名/参数按
// session_equipment.defaults.* 命名，响应体即 handler payload。
//
// get 返回合并视图：mcp=生效默认集（yaml 预置 ∪ state.json is_default 活标记，已按活性
// 过滤）；runtime=state.json 里所有被标记的（含断连）；unavailable=被标记但当前不可用的
// 子集；preset=config.yaml 部署预置层（运维管理，UI 只读展示）；inherit=false 表示
// config.yaml 整体关闭了该特性（此时 mcp 恒为空）；max=默认数量上限。
//
// set 是 PUT 语义：mcp 传"完整期望清单"（不是增量），后端在单锁内原子替换全部 is_default
// 标记；每个名字必须已连接（connected/connecting），否则 BAD_REQUEST。set 的响应只回
// mcp/runtime 两个键（preset/unavailable 等不在其中），调用方需要完整视图时 merge 旧值
// 或重新 get。

export interface EquipmentDefaultsView {
  mcp: string[];
  preset?: string[];
  runtime: string[];
  unavailable?: string[];
  inherit?: boolean;
  max?: number;
}

export async function getEquipmentDefaults(): Promise<EquipmentDefaultsView> {
  return webRequest<EquipmentDefaultsView>('session_equipment.defaults.get');
}

export async function setEquipmentDefaults(mcp: string[]): Promise<EquipmentDefaultsView> {
  return webRequest<EquipmentDefaultsView>('session_equipment.defaults.set', { mcp });
}
