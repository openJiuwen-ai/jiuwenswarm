/* ============================================================
 * 团队协作可视化 · 图布局
 * 只用快照里的 graph.layers（= 按 depth 分组）+ graph.node_order（稳定拓扑序）
 * 不自己算拓扑，避免与后端派生规则漂移。
 * ============================================================ */
(function (global) {
  'use strict';

  var TCV = (global.TCV = global.TCV || {});

  var NODE_W = 168;   // 节点宽（沿 main 轴之外的 cross 轴？见下）
  var NODE_H = 58;    // 节点高
  var GAP_MAIN = 74;  // 层间距（沿执行方向）
  var GAP_CROSS = 16; // 同层节点间距
  var PAD = 22;

  TCV.GRAPH_METRICS = { NODE_W: NODE_W, NODE_H: NODE_H, GAP_MAIN: GAP_MAIN, GAP_CROSS: GAP_CROSS, PAD: PAD };

  /**
   * @param {Array} tasks  快照 tasks[]
   * @param {Object} graph 快照 graph {layers, node_order}
   * @param {string} mode  'horizontal' | 'vertical'
   * @returns {{nodes:Object, width:number, height:number, mode:string, maxLayer:number}}
   *   nodes[task_id] = {x, y, w, h}（绝对坐标，左上角）
   *   horizontal: main 轴 = x（层从左到右），cross 轴 = y
   *   vertical:   main 轴 = y（层从上到下），cross 轴 = x
   */
  TCV.computeLayout = function (tasks, graph, mode, opts) {
    mode = mode === 'vertical' ? 'vertical' : 'horizontal';
    // 窄面板可以把节点调窄（同样画布能放更大字号），默认仍用 NODE_W
    var NW = (opts && typeof opts.nodeW === 'number' && opts.nodeW > 60) ? opts.nodeW : NODE_W;
    var list = tasks || [];
    var order = {};
    var nodeOrder = (graph && graph.node_order) || [];
    nodeOrder.forEach(function (id, i) { order[id] = i; });

    var byId = {};
    list.forEach(function (t) { if (t && t.task_id) byId[t.task_id] = t; });

    // 层：优先用后端 layers；缺失时退化为按 depth 自行分组
    var layers = [];
    if (graph && Array.isArray(graph.layers) && graph.layers.length) {
      layers = graph.layers.map(function (l) { return (l || []).filter(function (id) { return !!byId[id]; }); });
    } else {
      var byDepth = {};
      list.forEach(function (t) {
        var d = typeof t.depth === 'number' && t.depth >= 0 ? t.depth : 0;
        (byDepth[d] = byDepth[d] || []).push(t.task_id);
      });
      Object.keys(byDepth).map(Number).sort(function (a, b) { return a - b; })
        .forEach(function (d) { layers.push(byDepth[d]); });
    }

    // 兜底：layers 里没出现的任务补到最后一层，避免节点消失
    var placed = {};
    layers.forEach(function (l) { l.forEach(function (id) { placed[id] = true; }); });
    var missing = list.map(function (t) { return t.task_id; }).filter(function (id) { return id && !placed[id]; });
    if (missing.length) {
      if (!layers.length) layers.push([]);
      layers[layers.length - 1] = layers[layers.length - 1].concat(missing);
      missing.forEach(function (id) { placed[id] = true; });
    }

    // 同层顺序：按 node_order 稳定排序（未知者按 task_id）
    layers.forEach(function (layer) {
      layer.sort(function (a, b) {
        var oa = order[a], ob = order[b];
        if (oa === undefined && ob === undefined) return a < b ? -1 : a > b ? 1 : 0;
        if (oa === undefined) return 1;
        if (ob === undefined) return -1;
        return oa - ob;
      });
    });

    var maxCross = 0;
    layers.forEach(function (l) { if (l.length > maxCross) maxCross = l.length; });

    // 沿 main 轴 = 层的执行方向；沿 cross 轴 = 同层节点的排列方向。
    // 两个方向「占用的物理尺寸」不同：horizontal 时 main 走 x（用 NODE_W），
    // vertical 时 main 走 y（用 NODE_H）。写死一个会导致纵向布局同层节点重叠。
    var mainSize = mode === 'horizontal' ? NW : NODE_H;
    var crossSize = mode === 'horizontal' ? NODE_H : NW;

    var crossExtent = maxCross > 0 ? maxCross * crossSize + (maxCross - 1) * GAP_CROSS : 0;
    var mainExtent = layers.length > 0 ? layers.length * mainSize + (layers.length - 1) * GAP_MAIN : 0;

    var nodes = {};
    layers.forEach(function (layer, li) {
      var span = layer.length > 0 ? layer.length * crossSize + (layer.length - 1) * GAP_CROSS : 0;
      var crossStart = PAD + (crossExtent - span) / 2;
      layer.forEach(function (id, i) {
        var cross = crossStart + i * (crossSize + GAP_CROSS);
        var main = PAD + li * (mainSize + GAP_MAIN);
        if (mode === 'horizontal') {
          nodes[id] = { x: main, y: cross, w: NW, h: NODE_H };
        } else {
          nodes[id] = { x: cross, y: main, w: NW, h: NODE_H };
        }
      });
    });

    var width, height;
    if (mode === 'horizontal') {
      width = PAD * 2 + mainExtent;
      height = PAD * 2 + crossExtent;
    } else {
      width = PAD * 2 + crossExtent;
      height = PAD * 2 + mainExtent;
    }

    return {
      nodes: nodes,
      width: Math.max(width, NW + PAD * 2),
      height: Math.max(height, NODE_H + PAD * 2),
      mode: mode,
      maxLayer: layers.length
    };
  };

  /** 依赖边路径（三次贝塞尔） */
  TCV.edgePath = function (a, b, mode) {
    if (!a || !b) return '';
    if (mode === 'vertical') {
      var x1 = a.x + a.w / 2, y1 = a.y + a.h;
      var x2 = b.x + b.w / 2, y2 = b.y;
      var dy = Math.max(26, (y2 - y1) / 2);
      return 'M' + r(x1) + ',' + r(y1) +
        ' C' + r(x1) + ',' + r(y1 + dy) + ' ' + r(x2) + ',' + r(y2 - dy) + ' ' + r(x2) + ',' + r(y2);
    }
    var x1h = a.x + a.w, y1h = a.y + a.h / 2;
    var x2h = b.x, y2h = b.y + b.h / 2;
    var dx = Math.max(30, (x2h - x1h) / 2);
    return 'M' + r(x1h) + ',' + r(y1h) +
      ' C' + r(x1h + dx) + ',' + r(y1h) + ' ' + r(x2h - dx) + ',' + r(y2h) + ' ' + r(x2h) + ',' + r(y2h);
  };

  function r(n) { return Math.round(n * 10) / 10; }

})(window);
