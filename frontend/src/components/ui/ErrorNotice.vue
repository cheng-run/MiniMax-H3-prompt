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
.error-notice {
  padding: var(--space-3);
  border: 1px solid var(--color-error-text);
  background: var(--color-error-bg);
  color: var(--color-error-text);
  border-radius: var(--radius-md);
}
.error-notice__actions {
  margin-top: var(--space-2);
}
</style>
