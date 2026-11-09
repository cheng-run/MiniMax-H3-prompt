<template>
  <!-- role=status 让读屏软件播报加载变化；文字而非仅颜色表达状态。 -->
  <p role="status" class="loading-state">加载中…</p>
</template>

<style scoped>
.loading-state {
  color: var(--color-text-muted);
}
</style>
