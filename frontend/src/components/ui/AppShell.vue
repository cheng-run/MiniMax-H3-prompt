<script setup lang="ts">
import { Monitor, Moon, Sun } from '@lucide/vue'
import { computed, type Component } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { useTheme, type ThemeChoice } from '@/app/theme'

const route = useRoute()
const { choice, setChoice } = useTheme()

// 导航当前项按路径段判断，而不是只看 RouterLink 的精确匹配：详情页 `/sessions/<id>`
// 是独立路由记录，「会话」不至于在打开会话后失去下划线（会以为自己不在会话区）。
const onSessions = computed(() => route.path === '/' || route.path.startsWith('/sessions/'))
const onDiagnostics = computed(() => route.path.startsWith('/diagnostics'))

// 主题轮转顺序固定：跟随系统 → 深 → 浅。三态一个按钮走完，不做下拉菜单。
// 按钮显示的是**当前**选择；aria-label 同时给出下一档，动手前就知道会变成什么。
const ORDER: ThemeChoice[] = ['system', 'dark', 'light']
const ICONS: Record<ThemeChoice, Component> = { system: Monitor, dark: Moon, light: Sun }
const NAMES: Record<ThemeChoice, string> = { system: '跟随系统', dark: '深色', light: '浅色' }

const themeIcon = computed(() => ICONS[choice.value])
const nextChoice = computed(() => ORDER[(ORDER.indexOf(choice.value) + 1) % ORDER.length]!)
const themeLabel = computed(
  () => `主题：${NAMES[choice.value]}，点击切换到${NAMES[nextChoice.value]}`,
)

function cycleTheme(): void {
  setChoice(nextChoice.value)
}
</script>

<template>
  <!-- 跳转主内容链接：键盘用户第一个 Tab 就能拿到，跳过导航直达正文。 -->
  <a class="skip-link" href="#main-content">跳到主内容</a>

  <header class="app-header">
    <span class="app-brand">MiniMax H3 工作台</span>
    <span class="app-brand-rule" aria-hidden="true"></span>

    <nav class="app-nav" aria-label="主导航">
      <RouterLink
        to="/"
        class="app-nav__link"
        :class="{ 'is-current': onSessions }"
        :aria-current="onSessions ? 'page' : undefined"
        >会话</RouterLink
      >
      <RouterLink
        to="/diagnostics"
        class="app-nav__link"
        :class="{ 'is-current': onDiagnostics }"
        :aria-current="onDiagnostics ? 'page' : undefined"
        >诊断</RouterLink
      >
    </nav>

    <button type="button" class="theme-toggle" :aria-label="themeLabel" @click="cycleTheme">
      <component :is="themeIcon" :size="16" aria-hidden="true" />
    </button>
  </header>

  <!-- tabindex="-1" 让跳转链接的 #main-content 能真正把焦点移进来。 -->
  <main id="main-content" class="app-main" tabindex="-1">
    <slot />
  </main>
</template>

<style scoped>
/* 跳转链接默认移出视口，聚焦时回到可见位置；焦点环由全局 :focus-visible 提供。 */
.skip-link {
  position: absolute;
  left: -9999px;
  top: 0;
  z-index: 100;
  padding: var(--space-2) var(--space-3);
  background: var(--color-plate);
  border: 1px solid var(--color-rule-strong);
  border-radius: var(--radius-sm);
}
.skip-link:focus {
  left: var(--space-2);
}

/* 极窄顶栏：产品名、导航、主题切换都压在同一行，主内容是唯一的主角。 */
.app-header {
  display: flex;
  align-items: center;
  height: 56px;
  padding-inline: var(--space-6);
  background: var(--color-plate);
  border-bottom: 1px solid var(--color-rule);
}

/* 铭牌类小标签：全大写 + 宽字距是唯一允许加字距的位置。 */
.app-brand {
  font-size: var(--text-micro);
  letter-spacing: var(--track-wide);
  text-transform: uppercase;
  color: var(--color-ink);
  white-space: nowrap;
}
.app-brand-rule {
  flex: none;
  width: 1px;
  height: var(--space-3);
  margin-inline: var(--space-3);
  background: var(--color-rule);
}

.app-nav {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  height: 100%;
}
.app-nav__link {
  display: flex;
  align-items: center;
  position: relative;
  /* 下划线要落在文字下方一小段，而不是贴着顶栏底边（那会看起来像分隔线的一部分）。 */
  padding-block: var(--space-2);
  color: var(--color-ink-soft);
  transition: color var(--duration-fast) var(--ease-out);
}
.app-nav__link:hover {
  color: var(--color-ink);
}
/* 导航按下时同样给一次反馈：整条链接是跳转，没有按下态会像点了没反应。 */
.app-nav__link:active {
  opacity: 0.75;
}
/* 当前项：下方一道 2px 钠黄——全站唯一亮色只给「你在这」。 */
.app-nav__link.is-current {
  color: var(--color-ink);
}
.app-nav__link.is-current::after {
  content: '';
  position: absolute;
  inset-inline: 0;
  bottom: 0;
  height: 2px;
  background: var(--color-signal);
}

.theme-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  width: var(--space-5);
  height: var(--space-5);
  margin-inline-start: auto;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-ink-soft);
  cursor: pointer;
  transition:
    background-color var(--duration-fast) var(--ease-out),
    color var(--duration-fast) var(--ease-out);
}
.theme-toggle:hover {
  background: var(--color-plate-raised);
  color: var(--color-ink);
}
/* 按下时底色再压一档：三态轮转是「按一下换一个」，没有按下反馈会像没点中。 */
.theme-toggle:active {
  background: var(--color-plate);
}

.app-main {
  max-width: 1400px;
  margin-inline: auto;
  padding: var(--space-5) var(--space-6);
}
/* main 拿到焦点时不要额外的默认边框（它只是跳转落点，不是可交互控件）。 */
.app-main:focus {
  outline: none;
}

@media (max-width: 719.98px) {
  .app-header {
    padding-inline: var(--space-3);
  }
  /* 窄屏下产品名与导航挤在同一行：分隔线与间距各收一档，别让主题按钮被挤出去。 */
  .app-brand-rule {
    margin-inline: var(--space-2);
  }
  .app-nav {
    gap: var(--space-3);
  }
  .app-main {
    padding: var(--space-3);
  }
}
</style>
