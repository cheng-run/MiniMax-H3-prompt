<script setup lang="ts">
import { useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiError, getHealth, listSessions } from '@/api/client'
import Button from '@/components/ui/Button.vue'
import ErrorNotice from '@/components/ui/ErrorNotice.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'

import SessionCreateForm from './SessionCreateForm.vue'
import { statusLabel, statusTone } from './status'
import { useClipboardCopy } from './useClipboardCopy'

const PAGE_SIZE = 20

// 服务端只下发「下一页」游标，没有「上一页」指针；走过的游标自己存一份栈。
const cursors = ref<(string | null)[]>([null])
const pageIndex = ref(0)
const currentCursor = computed(() => cursors.value[pageIndex.value])

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: computed(() => ['sessions', { cursor: currentCursor.value }]),
  queryFn: () => listSessions({ limit: PAGE_SIZE, cursor: currentCursor.value }),
  retry: false,
})

const items = computed(() => data.value?.items ?? [])
const hasNext = computed(() => Boolean(data.value?.next_cursor))

// 页眉计数只数当前页：服务端给的是游标不是总数，把「本页 20 条」说成总数会在翻页时撒谎。
// 「要你动手」是本产品唯一必须由人干预的状态，单独挑出来用钠黄——列表页第一眼要回答的
// 就是「现在有几条等我」，而它不能靠数颜色，得有一句话。
const attentionCount = computed(
  () => items.value.filter((item) => item.status === 'awaiting_human').length,
)

// 导轨指示线按「行号」落位（translateY(行号 × 行高)），不追鼠标坐标：行高是契约常量，
// 这样键盘 Tab 也能顺手把那根线带到当前行。null = 指针不在任何一行上。
// 行号另存一份（pointerIndex）：隐去时线留在原地淡出，而不是先滑回第 0 行。
const hoverIndex = ref<number | null>(null)
const pointerIndex = ref(0)

function pointAt(index: number): void {
  pointerIndex.value = index
  hoverIndex.value = index
}

function nextPage(): void {
  const next = data.value?.next_cursor
  if (!next) {
    return
  }
  // 截断前进分支：先回退再前进时，旧的前向游标不该残留成第二条分叉。
  cursors.value = [...cursors.value.slice(0, pageIndex.value + 1), next]
  pageIndex.value += 1
  // 换页后行号全变了，旧指针会停在一条已经不存在的行上。
  hoverIndex.value = null
}

function prevPage(): void {
  if (pageIndex.value > 0) {
    pageIndex.value -= 1
    hoverIndex.value = null
  }
}

// 新建会话表单在列表页展开（spec §4）；展开是纯 UI 状态，随组件存亡。
const showForm = ref(false)

const errorMessage = computed(() =>
  error.value instanceof ApiError ? error.value.message : '加载失败，请稍后重试。',
)

// 缺密钥提示（#238 / spec #235 §7）：健康资源里的 missing_keys 非空就显示「去哪个
// 文件、按什么格式写」。读失败（服务异常）时**整块不出现**——提示是加法，不能因为
// 它把会话列表带崩或挡住已有的错误态。
const { data: health } = useQuery({ queryKey: ['health'], queryFn: getHealth, retry: false })
const missingKeys = computed(() => health.value?.missing_keys ?? [])
const envPath = computed(() => health.value?.env_path ?? '')
const configPath = computed(() => health.value?.config_path ?? '')

// 「复制路径」而不是「打开目录」（用户 2026-11-12 裁定）：打开资源管理器要新增端点、
// 还要处理 file:// 的策略差异；复制路径零依赖，用户自己粘贴到地址栏即可。
// 两处复制共用 useClipboardCopy（每次调用一份独立状态），模板变量名保持原样。
const { state: copyState, label: copyLabel, copy } = useClipboardCopy('复制路径')

function copyEnvPath(): Promise<void> {
  return copy(envPath.value)
}

// 实际地址（#237 / spec #235 §5.2、D5-AC2）：启动入口把 `http://127.0.0.1:<实际端口>/`
// 写进 app.state.base_url，健康资源原样下发。端口只在入口算一次，界面不再拼。
// 空串 = 未知（测试直接 create_app、或没按入口启动）——这时整块不显示，不显示假地址。
const baseUrl = computed(() => health.value?.base_url ?? '')

// 与密钥路径的复制按钮分开存状态：useClipboardCopy 每次调用互不影响，点一个按钮
// 不会把另一个的文案也改掉。
const {
  state: addressCopyState,
  label: addressCopyLabel,
  copy: copyAddress,
} = useClipboardCopy('复制')

function copyBaseUrl(): Promise<void> {
  return copyAddress(baseUrl.value)
}
</script>

<template>
  <section class="sessions">
    <header class="sessions__header">
      <div class="sessions__heading">
        <h1 class="sessions__title">会话</h1>
        <!-- 还没拿到数据时不写计数：这一行是事实陈述，宁可空着也不要闪一下 0。 -->
        <p v-if="!isPending && !isError" class="sessions__count">
          <span class="tnum">{{ items.length }}</span> 条记录
          <template v-if="attentionCount > 0">
            <span aria-hidden="true">·</span>
            <span class="sessions__count-attention">
              <span class="tnum">{{ attentionCount }}</span> 条要你动手
            </span>
          </template>
        </p>
      </div>
      <Button @click="showForm = !showForm">
        {{ showForm ? '收起新建' : '新建会话' }}
      </Button>
    </header>

    <!-- 铭牌行：本地地址与缺密钥提示都是「供抄写的一段路径」，不是警告条。 -->
    <section v-if="baseUrl" class="local-address">
      <p class="local-address__line">
        本地地址：<code>{{ baseUrl }}</code>
      </p>
      <Button variant="secondary" @click="copyBaseUrl">{{ addressCopyLabel }}</Button>
      <p v-if="addressCopyState === 'failed'" class="local-address__failed">
        复制失败：请手动选中上面的地址复制。
      </p>
    </section>

    <section v-if="missingKeys.length > 0" class="config-hint">
      <h2 class="config-hint__title">还没有配置模型密钥</h2>
      <p>本程序从下面这个文件读密钥，把缺的变量写进去（一行一个，值换成你自己的）：</p>
      <p class="config-hint__path">
        密钥文件：<code>{{ envPath }}</code>
      </p>
      <Button variant="secondary" @click="copyEnvPath">{{ copyLabel }}</Button>
      <p v-if="copyState === 'failed'" class="config-hint__failed">
        复制失败：请手动选中上面的路径复制。
      </p>
      <ul class="config-hint__keys">
        <li v-for="key in missingKeys" :key="key">
          <code>{{ key }}=你的密钥</code>
        </li>
      </ul>
      <p class="config-hint__path">
        配置文件：<code>{{ configPath }}</code>
      </p>
      <p class="config-hint__note">改完配置需重启程序才生效（本程序不热重载配置）。</p>
    </section>

    <SessionCreateForm v-if="showForm" />

    <!-- 波长轴：一页会话压成一行刻度，紧贴它要说明的那一批行（在本页导轨正上方）。
         纯装饰，读屏由页眉那句计数承担，这里 aria-hidden。
         :key 绑页码而不是会话 id：翻页时刻度树重挂载，落线动效跟着重播——
         「换了一批谱线，再曝光一次」。同一页内数据刷新不重播。 -->
    <div
      v-if="!isPending && !isError && items.length > 0"
      :key="pageIndex"
      class="spectrum"
      aria-hidden="true"
    >
      <span
        v-for="(item, index) in items"
        :key="item.session_id"
        class="spectrum__line"
        :data-status="item.status"
        :style="{ '--i': String(index) }"
      ></span>
    </div>

    <div class="sessions__body">
      <div class="rail" aria-hidden="true">
        <span class="rail__line"></span>
        <span
          class="rail__pointer"
          :class="{ 'rail__pointer--on': hoverIndex !== null }"
          :style="{ '--row-index': String(pointerIndex) }"
        ></span>
      </div>

      <div class="sessions__panel">
        <LoadingState v-if="isPending" />

        <ErrorNotice v-else-if="isError" :message="errorMessage">
          <Button @click="refetch()">重试</Button>
        </ErrorNotice>

        <template v-else>
          <p v-if="items.length === 0" class="sessions__empty">
            还没有会话，点击「新建会话」开始。
          </p>
          <ul v-else class="session-list" @mouseleave="hoverIndex = null">
            <li
              v-for="(item, index) in items"
              :key="item.session_id"
              class="session-item"
              :class="{ 'session-item--attention': item.status === 'awaiting_human' }"
              :data-status="item.status"
              :style="{ '--i': String(index) }"
              @mouseenter="pointAt(index)"
              @focusin="pointAt(index)"
              @focusout="hoverIndex = null"
            >
              <RouterLink class="session-link" :to="`/sessions/${item.session_id}`">
                <span class="session-title">{{ item.plot || item.session_id }}</span>
                <StatusBadge :tone="statusTone(item.status)" :label="statusLabel(item.status)" />
                <span class="session-end">
                  <span class="session-meta"
                    >{{ item.variant }} · 修订 <span class="tnum">{{ item.revision }}</span></span
                  >
                  <span v-if="item.status === 'awaiting_human'" class="session-next">NEXT</span>
                </span>
              </RouterLink>
            </li>
          </ul>
        </template>
      </div>
    </div>

    <div v-if="!isPending && !isError" class="pager">
      <p class="pager__index">
        第 <span class="tnum">{{ pageIndex + 1 }}</span> 页
      </p>
      <div class="pager__actions">
        <Button variant="secondary" :disabled="pageIndex === 0" @click="prevPage">上一页</Button>
        <Button variant="secondary" :disabled="!hasNext" @click="nextPage">下一页</Button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.sessions {
  /* 契约定死两个尺度：导轨列 56px、行高 44px（窄屏 28px），刻度长度 18px。间距令牌
     里没有这几档，但刻度必须落在行的垂直中心、行距必须等距，所以作为本品局部变量
     在这里各定义一次，只改这一处就整页对齐。 */
  --rail-w: 56px;
  --row-h: 44px;
  --tick-len: 18px;

  /* 满幅，不锁最大宽度（2026-11-21 复审：草图的构图是「摊开的板面」——
     行从导轨一路铺到右缘；此前锁 880px 居中，1440 下两侧各空 280px，行被挤成
     一条窄栏，与那口气质不是同一张图）。正文可读性由行的三段 grid 承担：
     标题列 minmax(0,1fr) 自己吃剩余宽度，状态与元信息靠右。 */
  max-width: none;
}

.sessions__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}
.sessions__heading {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}
/* 视觉标就是 h1（页面上只有这一个）。此前用 micro 字号 + 次要灰，比正文行还小，
   在 1440 下读起来像页脚注——它是这张板子的名字，不是注释（2026-11-22 复审）。
   提到 lead 档、骨白、字重 500。中文不加字距（rails-contract 明令）。 */
.sessions__title {
  font-size: var(--text-lead);
  font-weight: 500;
  line-height: 1.4;
  color: var(--color-ink);
}
.sessions__count {
  font-size: var(--text-micro);
  color: var(--color-ink-soft);
}
/* 计数是数据：走等宽数字，翻页时行里的数字不跳宽。 */
.sessions__count .tnum {
  font-family: var(--font-data);
}
.sessions__count-attention {
  color: var(--color-signal);
}

/* 波长轴：一页会话压成一行刻度。基线 1px，每个会话一根 2px 竖线挂在基线上（朝上），
   等待人工的那根加倍 + 钠黄，已死三态矮一截——与下面导轨的七态同构，只是缩成一条。
   它是页眉与列表之间的一条「谱」，不能贴住任何一侧：上下各留一档间距。 */
.spectrum {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  /* 高度要给最高的那根（等待人工 12px）留够，否则 flex-end + 固定高度会把它的
     上半截压扁；border-bottom 是那条轴本身。 */
  height: 14px;
  box-sizing: content-box;
  margin: var(--space-2) 0 var(--space-3);
  padding-left: var(--rail-w);
  border-bottom: 1px solid var(--color-rule);
}
.spectrum__line {
  flex: 0 0 2px;
  height: 8px;
  /* 刻度不直接坐在基线上：留 1px 空隙，否则它和 border-bottom 糊成一根粗线，
     远看像被裁掉一截（2026-11-21 目检）。 */
  margin-bottom: 1px;
  background: var(--color-state-live);
  /* 落线（DESIGN.md「签名动效：落线」）：刻度从基线向上长出来，按行号依次错开。
     变形原点在底边＝基线处，所以是「从轴上冒出来」而不是整体淡入。
     延迟按 index 递进、但封顶 --stagger-cap 格：一页 20 行时总时长仍是
     120ms(延迟) + 260ms(生长) = 380ms，不再随行数线性膨胀。 */
  transform-origin: 50% 100%;
  animation: spectrum-rise var(--duration-line) var(--ease-grow) both;
  animation-delay: calc(min(var(--i, 0), var(--stagger-cap)) * var(--stagger-line));
}
/* 从 0 高 → 自身高度。用 scaleY 而不是 height：不触发布局，20 行同时跑也不掉帧。 */
@keyframes spectrum-rise {
  from {
    transform: scaleY(0);
  }
  to {
    transform: scaleY(1);
  }
}
.spectrum__line[data-status='completed'] {
  height: 5px;
  background: var(--color-state-done);
}
.spectrum__line[data-status='created'] {
  background: var(--color-state-pending);
}
.spectrum__line[data-status='cancelled'] {
  background: var(--color-state-dead);
}
.spectrum__line[data-status='interrupted'] {
  background: var(--color-state-done);
}
.spectrum__line[data-status='failed'] {
  background: var(--color-error);
}
.spectrum__line[data-status='awaiting_human'] {
  flex-basis: 5px;
  height: 12px;
  background: linear-gradient(
    to right,
    var(--color-signal) 0 2px,
    transparent 2px 3px,
    var(--color-signal) 3px 5px
  );
}

/* 铭牌行：本地地址与缺密钥提示都是「供抄写的一段路径」，不是警告条。
   1px 发光线 + 板面底，与列表读作同一张板子。 */
.local-address,
.config-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-3);
  margin-bottom: var(--space-3);
  padding: var(--space-2) var(--space-3);
  background: var(--color-plate);
  border: 1px solid var(--color-rule);
  border-radius: var(--radius-sm);
  font-size: var(--text-micro);
  color: var(--color-ink-soft);
}
.config-hint {
  /* 密钥提示是多行说明，竖着排；底色与线仍与地址铭牌同一种，不另造形态。 */
  flex-direction: column;
  align-items: flex-start;
}
.local-address__line {
  flex: 1 1 auto;
}
.local-address p,
.config-hint p {
  margin: 0;
  overflow-wrap: anywhere;
}
.local-address code,
.config-hint code {
  font-family: var(--font-data);
  color: var(--color-ink);
}
.local-address__failed,
.config-hint__failed {
  color: var(--color-error);
}
.config-hint__title {
  margin: 0;
  font-size: var(--text-micro);
  font-weight: 500;
  color: var(--color-ink);
}
.config-hint__keys {
  margin: 0;
  padding-left: var(--space-3);
}

/* 主体：左导轨 + 右板面同一个 grid，刻度线才能靠尺寸关系与行对齐。 */
.sessions__body {
  display: grid;
  grid-template-columns: var(--rail-w) 1fr;
}
.rail {
  position: relative;
}
/* 纵向基线：一根 1px 发光线取代整页的边框。grid 默认拉伸，空列表时它照样贯通。 */
.rail__line {
  position: absolute;
  left: 50%;
  top: 0;
  bottom: 0;
  width: 1px;
  background: var(--color-rule);
}
/* 全站唯一的签名动效：指示线落到被指向的那一行上，过冲一次再稳住（rail-contract）。
   行号由每行的 mouseenter/focusin 写成 --row-index，transform 才可插值；
   别处一律 --ease-out / --duration-fast。线从基线向右伸一格，读作「这一行」。

   默认不可见：没有指向任何行时它不该在首行露头（早先只靠 opacity:0 + 立即 transition，
   首帧会闪一下）。visibility 参与过渡，隐藏时彻底不参与绘制与命中。 */
.rail__pointer {
  position: absolute;
  left: 50%;
  top: 0;
  width: calc(var(--tick-len) + var(--space-2));
  height: 1px;
  background: var(--color-signal);
  opacity: 0;
  visibility: hidden;
  /* 与刻度同芯跨在基线上：指向哪一行，哪一行就被钠黄穿过一次。 */
  transform: translate(-50%, calc(var(--row-index, 0) * var(--row-h)));
  transition:
    transform var(--duration-base) var(--ease-expo),
    opacity var(--duration-fast) var(--ease-out),
    visibility 0s linear var(--duration-fast);
}
.rail__pointer--on {
  opacity: 1;
  visibility: visible;
  transition:
    transform var(--duration-base) var(--ease-expo),
    opacity var(--duration-fast) var(--ease-out),
    visibility 0s;
}
.sessions__panel {
  min-width: 0;
}

.sessions__empty {
  padding-block: var(--space-4);
  color: var(--color-ink-soft);
}

/* 行：不用卡片，靠 1px 分隔线；悬停整行压一档底色。
   高度写在 li 上（含 1px 边框），44px 才是真实行距，与指示线的步进一致。 */
.session-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.session-item {
  position: relative;
  height: var(--row-h);
  border-bottom: 1px solid var(--color-rule);
  transition: background-color var(--duration-fast) var(--ease-out);
}
.session-item:hover {
  background: var(--color-plate);
}
/* 按下反馈：整行是跳转链接，按下时底色再压一档，松手即离开。
   写在 :hover 之后、与 attention 行的那对规则并列，保证悬停底色不被覆盖。 */
.session-item:active {
  background: var(--color-plate-raised);
}
.session-link {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: var(--space-4);
  height: 100%;
  /* 满幅布局下，超宽屏幕上标题会横跨整个屏（120ch 以上），读长句要来回甩头。
     给行内容一个可读上限：超过就留白，不硬拉。 */
  max-width: 1500px;
  color: var(--color-ink);
  font-size: var(--text-body);
}
.session-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 能点的信号来自下划线本身（纸面铅笔划线的逻辑），不是蓝字。 */
.session-item:hover .session-title {
  text-decoration: underline;
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
  text-decoration-color: var(--color-rule-strong);
}
/* 元信息与行尾铭牌是同一个「行末」区，3 列契约不被 NEXT 撑出第四条空轨。 */
.session-end {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.session-meta {
  font-family: var(--font-data);
  font-size: var(--text-micro);
  color: var(--color-ink-soft);
  white-space: nowrap;
}
/* 行尾铭牌只出现在等待人工那一行；英语小件，可以带字距。 */
.session-next {
  font-size: var(--text-micro);
  letter-spacing: var(--track-wide);
  color: var(--color-signal);
  white-space: nowrap;
}
/* 「等你动手」整行：浮起一档底，标题换钠黄。写在 :hover 之后且补一条 hover 规则——
   否则 .session-item:hover 的特异性更高，悬停时会把这一行压回普通底色。
   :active 也要点名：等待人工那行本来就停在 plate-raised，按下时改压 plate，
   否则按下去与悬停像素级同貌，读起来像没点中。 */
.session-item--attention,
.session-item--attention:hover {
  background: var(--color-plate-raised);
}
.session-item--attention:active {
  background: var(--color-plate);
}
.session-item--attention .session-title {
  color: var(--color-signal);
}

/* 刻度线挂在行自身上（而不是另造一列等高的 li）：行高变化时刻度仍落在行的垂直中心，
   「线与线间距 44px」由行高本身保证。形态按 rail-contract 的七态表。

   方向：基线是**纵向**的，所以刻度必须**横向**伸出（18px 长、2px 粗），跨在基线上读作
   「这一行在这里」。早先写成纵向 2px 粗块压在竖线上，读起来是一个方点、像「盒子一侧
   的色条」——那是 craft floor 明令禁止的形态。 */
.session-item::before {
  content: '';
  position: absolute;
  left: calc(var(--rail-w) * -0.5);
  top: 50%;
  width: var(--tick-len);
  height: 2px;
  /* 跨在纵向基线上、左右各伸一半：刻度是「轴上的刻度」，不是贴边的一截。 */
  transform: translate(-50%, -50%);
  background: var(--color-state-live);
  /* 落线：行刻度从基线上向两侧伸出，与波长轴同一次手势、同一串延迟（同样封顶）。
     用 clip-path 而不是 scaleX——刻度的宽度承载状态（已完成是半高），
     scaleX 会把它压扁成另一种长度；clip-path 只裁可见范围，形态不变。
     原点在横向正中（基线所在），所以是「从轴上向两边长」。 */
  clip-path: inset(-2px 50% -2px 50%);
  animation: tick-draw var(--duration-line) var(--ease-grow) both;
  animation-delay: calc(min(var(--i, 0), var(--stagger-cap)) * var(--stagger-line));
}
@keyframes tick-draw {
  from {
    clip-path: inset(-2px 50% -2px 50%);
  }
  to {
    clip-path: inset(-2px -2px -2px -2px);
  }
}
/* 七态线形（rail-contract 的表）：实线全高 / 双线 / 半高 / 叠加斜划 / 极暗。 */
.session-item[data-status='created']::before {
  background: var(--color-state-pending);
}
.session-item[data-status='completed']::before {
  /* 半高：18px → 9px，仍跨在基线上。 */
  width: calc(var(--tick-len) / 2);
  background: var(--color-state-done);
}
.session-item[data-status='interrupted']::before {
  background: var(--color-state-done);
}
.session-item[data-status='cancelled']::before {
  background: var(--color-state-dead);
}
.session-item[data-status='failed']::before {
  background: var(--color-error);
}
/* 等待人工：两根 2px 平行、间隔 2px，钠黄——全站唯一亮色只给这一条。 */
.session-item[data-status='awaiting_human']::before {
  height: 6px;
  background: linear-gradient(
    to bottom,
    var(--color-signal) 0 2px,
    transparent 2px 4px,
    var(--color-signal) 4px 6px
  );
}
/* 死掉的三态：整根刻度**自己**倾斜成斜线，而不是在横刻度上再叠一根斜线——
   叠加会读成一个「>」箭头（早先实测：首行被误认成可展开）。与其它刻度同芯旋转，
   仍跨在基线上。 */
.session-item[data-status='interrupted']::before,
.session-item[data-status='cancelled']::before,
.session-item[data-status='failed']::before {
  height: 1.5px;
  transform: translate(-50%, -50%) rotate(-45deg);
}
/* 删除线只在「已中断 / 已取消」的标题上：这两态的故事线已经断了
   （品红色不在本世界，舍弃）。标题仍用 ink-soft（7.78:1）——「冲淡」交给删除线
   与刻度形态就够了；此前退到 ink-faint 只有 3.94:1，整行读不清（2026-11-21 复审）。 */
.session-item[data-status='interrupted'] .session-title,
.session-item[data-status='cancelled'] .session-title {
  color: var(--color-ink-soft);
  text-decoration: line-through;
  text-decoration-color: var(--color-rule-strong);
}

/* 分页落在列表右下角：页码说明是一行 micro 文字，按钮在它右侧。
   左缘让出导轨列，与列表行的起点对齐。 */
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-3);
  padding-left: var(--rail-w);
  margin-top: var(--space-3);
}
.pager__index {
  font-size: var(--text-micro);
  color: var(--color-ink-soft);
}
.pager__index .tnum {
  font-family: var(--font-data);
}
.pager__actions {
  display: flex;
  gap: var(--space-2);
}

/* 窄屏：导轨收窄，行折成两行（标题占满第一行，状态与元信息第二行），不出横向滚动。
   44px 步进与悬停指示线都是鼠标桌面的语法，触屏上整条关掉。 */
@media (max-width: 720px) {
  .sessions__body {
    /* 窄屏刻度从基线向左伸，导轨列只有 28px，超出的那截会跑到视口外（左缘露半根
       斜线）。给整块留裁剪边界，刻度仍然贴在基线上。
       注意：波长轴不在这个容器里，别把它的上半截一起裁掉。 */
    overflow: hidden;
  }
  .sessions {
    --rail-w: 28px;
    --tick-len: 12px;
  }
  /* 刻度右端仍顶在基线上（基线在导轨列 50% 处）：把 left 从 -rail-w/2 改成
     -rail-w/2 + 一点点，让 12px 刻度整根落在导轨列内。
     早先图省事写 left: 0，刻度就从基线**脱开**悬在左侧 8px 处
     （2026-11-21 复审实测：窄屏是悬空的小横杠，与轴断开）。 */
  .session-item::before {
    left: calc(var(--rail-w) * -0.5 + var(--tick-len));
  }
  .session-item[data-status='interrupted']::before,
  .session-item[data-status='cancelled']::before,
  .session-item[data-status='failed']::before {
    transform: translate(-100%, -50%) rotate(-45deg);
  }
  .rail__pointer {
    display: none;
  }
  .session-item {
    height: auto;
  }
  .session-link {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-3);
    padding-block: var(--space-2);
  }
  .session-title {
    flex: 0 0 100%;
  }
  .session-end {
    /* 元信息推到第二行右端，标题留下的宽度归状态徽标。 */
    margin-left: auto;
  }
  .pager {
    padding-left: 0;
  }
}

/* 减少动效：落线是「从零长出来」，属于位移/绘制型，整条关掉，刻度直接以终态出现。
   全局 reset 已把 animation-delay 归零，这里再把 clip-path/scaleY 复位，
   保证没有中间态残留（否则定格在 scaleY(0) 会整条看不见）。 */
@media (prefers-reduced-motion: reduce) {
  .spectrum__line,
  .session-item::before {
    animation: none;
  }
  .spectrum__line {
    transform: none;
  }
  .session-item::before {
    clip-path: none;
  }
}
</style>
