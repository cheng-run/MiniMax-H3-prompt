import { defineStore } from 'pinia'
import { ref } from 'vue'

// Pinia 只保存客户端 UI 状态（D1 spec §6）：面板展开/收起之类，绝不复制服务器业务状态。
// D1 用在诊断页的「详情展开/收起」作为最小可执行示例。
export const useUiStore = defineStore('ui', () => {
  const diagnosticsExpanded = ref(false)

  function toggleDiagnostics() {
    diagnosticsExpanded.value = !diagnosticsExpanded.value
  }

  return { diagnosticsExpanded, toggleDiagnostics }
})
