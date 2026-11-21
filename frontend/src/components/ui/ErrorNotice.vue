<script setup lang="ts">
defineProps<{ message: string }>()
</script>

<template>
  <!-- role=alert 让读屏软件立即播报错误；操作（如重试）由使用方经默认插槽注入。 -->
  <div role="alert" class="error-notice">
    <p class="error-notice__message">{{ message }}</p>
    <div v-if="$slots.default" class="error-notice__actions">
      <slot />
    </div>
  </div>
</template>

<style scoped>
/* 整块用全边框而不是一侧色条：一侧色条在圆角盒上读起来像装饰，
   全边框才和「1px 发光线取代边框与阴影」的语法一致。 */
.error-notice {
  padding: var(--space-3);
  background: var(--color-plate);
  border: 1px solid var(--color-error);
  border-radius: var(--radius-sm);
  color: var(--color-error);
  font-size: var(--text-body);
}
.error-notice__message {
  margin: 0;
}
.error-notice__actions {
  margin-top: var(--space-2);
}
</style>
