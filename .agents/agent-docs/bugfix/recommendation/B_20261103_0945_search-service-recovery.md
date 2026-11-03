---
id: B_20261103_0945_search-service-recovery
title: 搜索配置恢复后重新创建话题推荐服务
type: B
author: Rossoneri
author_email: 863031676@qq.com
modules:
  - jiuwenswarm/server/runtime
files:
  - jiuwenswarm/server/runtime/recommendation_host.py
  - tests/unit_tests/test_topic_recommendation.py
designs: []
design_note: 修复现有推荐服务在搜索配置恢复后无法重新初始化的问题，无独立设计。
---

# 搜索配置恢复后重新创建话题推荐服务

## 现象与根因

2026-11-03 本机新对话页开关已打开，但 topic_starters.get 返回 topic_service_unavailable。
启动日志显示 openjiuwen_native_search_requires_free_engines：DDG、Bing 均关闭。
RecommendationHost 将失败初始化记为已完成，后续 ensure_services 直接返回，配置恢复也无法创建服务。

## 修复与契约

保留搜索配置校验，不自动绕过搜索来源开关。已初始化但产品服务缺失时，重新检查搜索配置；配置有效后重试组装产品服务。
重试复用准备服务、存储和结果队列，不丢弃进行中的准备任务或待处理结果。成功后绑定推送回调；后续读取复用已恢复的服务。
中英文使用说明补充所需搜索配置和恢复行为。

## 验证

- Windows、项目 .venv：pytest tests/unit_tests/test_topic_recommendation.py -q --no-cov，69 项通过。
- 新增回归覆盖搜索关闭、只开一个来源、两个来源开启后恢复、已有队列及准备服务保持、推送回调绑定、重复读取不重建。
- 既有不可用服务测试显式关闭搜索开关，避免用户实际配置污染测试预期。
- 本机通过 config.save_all 启用 DDG、Bing，并重启已确认的 Runtime Worker 加载修改。
- 经 localhost:5173/ws 调用 topic_starters.status 返回 enabled=true、status=ready；topic_starters.get 返回 available=true 和 4 张真实接口提供的话题卡片。
- 上述卡片包含冷启动语料；接口可用不等于已验证个性化模型生成和外网搜索的完整成功链路。

## 遗留

浏览器最终展示待页面刷新核对；不以接口响应代替界面截图证据。
