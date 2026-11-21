<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">数据质量监控与元数据治理平台</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">面向数据质量标准定义、自动巡检、异常告警、元数据采集与数据目录构建的数据质量治理平台。</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "质量报告跨版本核对", path: "/report-compat" }, { label: "质量规则定义", path: "/dq_rule_def" }, { label: "巡检任务管理", path: "/dq_check_run" }, { label: "质量事件管理", path: "/dq_incident" }, { label: "元数据采集器", path: "/metadata_crawler" }, { label: "数据表目录", path: "/table_catalog" }, { label: "字段画像分析", path: "/column_profile" }, { label: "血缘图谱", path: "/lineage_graph" }, { label: "指标波动监控", path: "/metric_monitor" }, { label: "质量评分卡", path: "/dq_scorecard" }, { label: "数据分类标签", path: "/tag_classify" }, { label: "规则模板库", path: "/dq_template" }, { label: "异常模式识别", path: "/anomaly_pattern" }, { label: "质量报告生成", path: "/dq_report" }, { label: "元数据变更对比", path: "/metadata_diff" }, { label: "告警通知通道", path: "/dq_alert_channel" }, { label: "质量基线管理", path: "/dq_baseline" }, { label: "主数据实体注册", path: "/mdm_entity" }, { label: "质量趋势看板", path: "/dq_trend" }]
</script>
