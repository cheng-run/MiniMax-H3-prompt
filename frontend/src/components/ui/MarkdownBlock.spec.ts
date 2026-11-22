import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import MarkdownBlock from './MarkdownBlock.vue'

// 产物正文的只读渲染块：把 markdown 排版出来，但**绝不**执行正文里的标记。
//
// 两条边界刻在用例里，因为它们是实测踩到的：
// ① 产物正文有 770 处 `<Picture N>` / `<Subject N>` / `<d>x</d>`，是占位符不是 HTML，
//    渲染后必须仍按字面出现（允许原始 HTML 就会把它们吞掉）；
// ② 正文来自模型，不是可信 HTML，`<script>` 与 `javascript:` 链接都不能成为活跃节点。
function render(text: string) {
  return mount(MarkdownBlock, { props: { text } })
}

describe('MarkdownBlock 渲染', () => {
  it('标题与粗体按排版出现，记号本身不再显示', () => {
    const wrapper = render('## 场 1\n\n**场景**：明代闺阁')

    expect(wrapper.get('h2').text()).toBe('场 1')
    expect(wrapper.get('strong').text()).toBe('场景')
    expect(wrapper.text()).not.toContain('##')
    expect(wrapper.text()).not.toContain('**')
  })

  it('无序列表与嵌套缩进都按列表渲染', () => {
    const wrapper = render('- **0–1s｜停住**：人物已倚在窗边\n- 外层\n  - 内层条目')

    const items = wrapper.findAll('li')
    expect(items.length).toBeGreaterThanOrEqual(3)
    expect(wrapper.findAll('li li')).toHaveLength(1)
    expect(wrapper.get('li strong').text()).toBe('0–1s｜停住')
  })

  it('表格与行内代码按结构渲染', () => {
    const wrapper = render('| 镜头 | 帧数 |\n|---|---|\n| 1 | 107 |\n\n参数 `30:17`')

    expect(wrapper.findAll('th')).toHaveLength(2)
    expect(wrapper.findAll('td')).toHaveLength(2)
    expect(wrapper.get('code').text()).toBe('30:17')
  })

  it('分割线与引用块按线渲染', () => {
    const wrapper = render('上\n\n---\n\n> 一句引用')

    expect(wrapper.find('hr').exists()).toBe(true)
    expect(wrapper.get('blockquote').text()).toContain('一句引用')
  })
})

describe('MarkdownBlock 不许执行正文里的标记', () => {
  it('<Picture N> / <Subject N> / <d>x</d> 占位符按字面显示，不被当成标签吞掉', () => {
    // 这 770 处是提示词接线要照抄的编号，丢了用户就接不上图。
    const wrapper = render('接 <Picture 1> 与 <Subject 5>，标签写着 <d>西藏</d>')

    expect(wrapper.text()).toContain('<Picture 1>')
    expect(wrapper.text()).toContain('<Subject 5>')
    expect(wrapper.text()).toContain('<d>西藏</d>')
    // 没有被解析成真元素
    expect(wrapper.find('d').exists()).toBe(false)
  })

  it('<script> 与其它原始 HTML 不会成为活跃节点', () => {
    const wrapper = render(
      '正常一行\n\n<script>window.__pwned = true</script>\n\n<img src=x onerror="x">',
    )

    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('<script>')
  })

  it('javascript: 链接不会变成可点的锚', () => {
    const wrapper = render('[点我](javascript:alert(1))')

    expect(wrapper.find('a').exists()).toBe(false)
  })

  it('http(s) 外链新标签打开并切断 opener', () => {
    const wrapper = render('[文档](https://example.com/a)')

    const link = wrapper.get('a')
    expect(link.attributes('href')).toBe('https://example.com/a')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
  })
})

describe('MarkdownBlock 不破坏正文语义', () => {
  it('标识符里的下划线不触发斜体（integrated_multimodal_description 等 941 处）', () => {
    const wrapper = render('integrated_multimodal_description: 文本')

    expect(wrapper.find('em').exists()).toBe(false)
    expect(wrapper.text()).toContain('integrated_multimodal_description')
  })

  it('单换行真的换行（提示词按行组织，挤成一段会读错）', () => {
    const wrapper = render('第一行\n第二行')

    expect(wrapper.findAll('br').length).toBeGreaterThanOrEqual(1)
  })

  it('空正文渲染成空块而不是报错', () => {
    const wrapper = render('')

    expect(wrapper.find('.markdown-block').exists()).toBe(true)
    expect(wrapper.text()).toBe('')
  })
})
