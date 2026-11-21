<template>
  <!-- role=status 让读屏软件播报加载变化；文字而非仅颜色表达状态。
       文案沿用「正在加载中」——既有用例与读屏播报都按这句断言。 -->
  <p role="status" class="loading-state">
    <span class="loading-state__track" aria-hidden="true"><i></i></span>
    <span>正在加载中…</span>
  </p>
</template>

<style scoped>
.loading-state {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin: 0;
  color: var(--color-ink-soft);
}

/* 跑动的线：所有「还在跑」在视觉上都是同一种运动，读起来不刺眼。
   宽度固定，移动靠 transform；reduced-motion 由全局 reset 压掉。 */
.loading-state__track {
  position: relative;
  flex: none;
  width: 64px;
  height: 1px;
  overflow: hidden;
  background: var(--color-rule);
}
.loading-state__track i {
  position: absolute;
  top: 0;
  left: 0;
  width: 40px;
  height: 1px;
  background: var(--color-rule-strong);
  animation: loading-drift 1.4s var(--ease-out) infinite;
}

@keyframes loading-drift {
  0% {
    transform: translateX(-100%);
    background: var(--color-rule-strong);
  }
  50% {
    background: var(--color-ink-soft);
  }
  100% {
    transform: translateX(160%);
    background: var(--color-rule-strong);
  }
}
</style>
