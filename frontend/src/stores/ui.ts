import { defineStore } from 'pinia'
import { ref } from 'vue'

// Pinia 只保存客户端 UI 状态（D1 spec §6）：面板展开/收起、工作台分区选择之类，
// 绝不复制服务器业务状态——业务状态只来自快照 query（D4 spec §6.1）。
export type WorkbenchTab = 'overview' | 'review' | 'segments' | 'assets' | 'artifacts'

export const useUiStore = defineStore('ui', () => {
  const diagnosticsExpanded = ref(false)

  function toggleDiagnostics() {
    diagnosticsExpanded.value = !diagnosticsExpanded.value
  }

  // 工作台分区：页内状态、不进 URL（D4-AC4）。刷新 = 新的 Pinia = 回到概览；
  // 切换会话时由视图重置，避免上一个会话看的分区在另一个会话上「继承」。
  const workbenchTab = ref<WorkbenchTab>('overview')

  function selectWorkbenchTab(tab: WorkbenchTab) {
    workbenchTab.value = tab
  }

  return { diagnosticsExpanded, toggleDiagnostics, workbenchTab, selectWorkbenchTab }
})
