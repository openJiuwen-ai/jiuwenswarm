---
id: B_20261126_2025_installed-app-icon-expiry
title: Hub 应用安装后的图标链接过期
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
files:
  - jiuwenswarm/extensions/app_market/app_icon.py
  - jiuwenswarm/extensions/app_market/hub_install.py
  - jiuwenswarm/extensions/app_market/manager.py
  - tests/unit_tests/test_app_market_hub_install.py
designs: []
design_note: 修复本地展示依赖临时链接的问题，沿用 S_03 的 Hub 应用包装和登记契约。
---

# Hub 应用安装后的图标链接过期

Hub 的目录和详情使用对象存储签名 URL。安装和更新把该 URL 写入本地清单，「我的应用」在签名过期后无法加载图标。黄沙镇往事、轨道之家和篝火狼人的安装目录已经包含入口 HTML 引用的 PNG。

新安装及更新使用已校验制品里的图标数据。旧安装在列表响应中读取已有应用目录内的图标，枚举时无需改写清单。旧安装如果只在原包外层携带图标且本地没有入口图标，需重新下载制品才有可用图片。来源优先级及大小约束见 [Hub 应用分发契约](../../../../specs/jiuwenswarm/extensions/app_market/S_03_app-hub-distribution.md)。

Windows 项目 `.venv` 定向回归 5 项通过，覆盖安装读取外层图标、更新改用新版本入口图标、旧清单回退和越界路径、转为本地、更新失败回滚。使用三份真实已安装清单的副本调用应用列表入口，确认响应的三张 PNG 与现有安装目录内的图片逐字节一致。安装及更新回归使用模拟 Hub。
