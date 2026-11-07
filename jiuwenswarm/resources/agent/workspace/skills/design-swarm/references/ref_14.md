# 🔄 五步闭环流程

## 🔄 五步闭环流程

### Phase 1：需求确认

**输入源：**

| 输入 | 来源 | 处理 | 输出 |
|------|------|------|------|
| 户型数据 | smart-home-dxf-pro | 房间信息提取 | 房间清单 |
| 设备清单 | quote-swarm | 设备数量+型号 | 设备矩阵 |
| 设计要求 | 客户/爽哥 | 风格确认 | 设计规范 |

---

### Phase 2：点位规划

**规划命令：**

```bash
# 点位图生成
@smart-home-floorplan
  输入：房间信息+设备清单
  参数：
    style: [现代/简约/轻奢]
    colorScheme: [标准/品牌]
  输出：设备点位图PNG

# 图标生成
@smart-home-icons
  输入：设备类型
  输出：SVG图标集
```

---

### Phase 3：布线设计

**设计命令：**

```bash
# 布线图生成
@smart-home-wiring
  输入：点位图+设备清单
  参数：
    powerCalculation: true
    circuitAllocation: true
    gatewayCoverage: true
  输出：智能布线图PNG+回路分配表
```

**布线规范：**

| 规范 | 要求 | 检查 |
|------|------|------|
| 强弱电分离 | 强电/弱电间距≥30cm | 图纸核对 |
| 回路分配 | 每回路≤8个设备 | 数量核对 |
| 电源冗余 | ≥20%冗余 | 功耗计算 |
| 网关覆盖 | 每层≥1个中枢 | 覆盖范围 |

---

### Phase 4：3D渲染

**渲染命令：**

```bash
# 3D效果图生成
@lobster-3d-studio
  输入：户型数据+设备点位
  参数：
    resolution: 1080p
    viewpoints: [客厅/卧室/厨房/卫生间]
    lighting: [自然光/暖光/冷光]
  输出：3D效果图PNG
```

---

### Phase 5：交付打包

**交付清单：**

| 交付物 | 工具 | 说明 |
|--------|------|------|
| 设备点位图 | smart-home-floorplan | 设备布局 |
| 智能布线图 | smart-home-wiring | 布线方案 |
| 3D效果图 | lobster-3d-studio | 效果展示 |
| 回路分配表 | smart-home-wiring | 电气方案 |
| 设备清单 | JSON | 数据汇总 |

---


