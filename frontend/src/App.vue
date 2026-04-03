<script setup>
import { ref } from 'vue'
import ConversationList from './components/ConversationList.vue'
import MessageList from './components/MessageList.vue'
import SearchBar from './components/SearchBar.vue'

const activeConversation = ref(null)
const searchHighlight = ref('')
const jumpToSeq = ref(null)
const sidebarOpen = ref(false)

const themes = [
  { id: 'dark',   label: '深蓝', color: '#1a1a2e' },
  { id: 'wechat', label: '微信', color: '#95ec69' },
  { id: 'light',  label: '浅色', color: '#ffffff' },
  { id: 'warm',   label: '暖棕', color: '#8b5e3c' },
  { id: 'purple', label: '紫夜', color: '#6a2fad' },
]
const currentTheme = ref(localStorage.getItem('theme') || 'dark')
applyTheme(currentTheme.value)

function applyTheme(id) {
  currentTheme.value = id
  document.documentElement.setAttribute('data-theme', id === 'dark' ? '' : id)
  localStorage.setItem('theme', id)
}

function selectConversation(conv) {
  activeConversation.value = conv
  searchHighlight.value = ''
  sidebarOpen.value = false
}

function navigateToMessage(item) {
  activeConversation.value = {
    conv_id: item.conv_id,
    name: item.conv_name || '未知',
  }
  searchHighlight.value = item.content?.substring(0, 20) || ''
  jumpToSeq.value = item.seq || null
}
</script>

<template>
  <div class="app-layout">
    <div class="sidebar-overlay" :class="{ visible: sidebarOpen }" @click="sidebarOpen = false"></div>
    <div class="app-sidebar" :class="{ open: sidebarOpen }">
      <ConversationList
        :activeId="activeConversation?.conv_id"
        @select="selectConversation"
      />
    </div>
    <div class="app-main">
      <div class="app-toolbar">
        <button class="sidebar-toggle" @click="sidebarOpen = !sidebarOpen">☰</button>
        <div class="app-title">抖音聊天记录</div>
        <SearchBar @navigate="navigateToMessage" />
        <div class="theme-switcher">
          <button
            v-for="t in themes"
            :key="t.id"
            class="theme-btn"
            :class="{ active: currentTheme === t.id }"
            :title="t.label"
            :style="{ background: t.color }"
            @click="applyTheme(t.id)"
          />
        </div>
      </div>
      <MessageList
        :conversation="activeConversation"
        :searchHighlight="searchHighlight"
        :jumpToSeq="jumpToSeq"
        @jumped="jumpToSeq = null"
      />
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  height: 100vh;
}

.app-sidebar {
  width: 300px;
  flex-shrink: 0;
}

.app-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  overflow: hidden;
}

.app-toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 20px;
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border-color);
}

.app-title {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
  color: var(--accent);
}

.app-toolbar .search-container {
  flex: 1;
  max-width: 400px;
}

.theme-switcher {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-left: auto;
}

.theme-btn {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: 2px solid transparent;
  cursor: pointer;
  transition: transform 0.15s, border-color 0.15s;
}

.theme-btn:hover {
  transform: scale(1.2);
}

.theme-btn.active {
  border-color: var(--accent);
  transform: scale(1.15);
}

.sidebar-toggle {
  display: none;
  background: none;
  border: none;
  color: var(--text-primary);
  font-size: 22px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  flex-shrink: 0;
}
.sidebar-toggle:hover {
  background: var(--bg-tertiary);
}

.sidebar-overlay {
  display: none;
}

@media (max-width: 768px) {
  .sidebar-toggle {
    display: block;
  }

  .app-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    height: 100vh;
    z-index: 1000;
    transform: translateX(-100%);
    transition: transform 0.25s ease;
    width: 280px;
  }

  .app-sidebar.open {
    transform: translateX(0);
  }

  .sidebar-overlay {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 999;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s ease;
  }

  .sidebar-overlay.visible {
    opacity: 1;
    pointer-events: auto;
  }

  .app-toolbar {
    flex-wrap: wrap;
    padding: 8px 12px;
    gap: 8px;
  }

  .app-title {
    flex: 1;
    font-size: 14px;
  }

  .theme-switcher {
    order: 0;
    margin-left: 0;
    gap: 5px;
  }

  .app-toolbar .search-container {
    order: 1;
    flex-basis: 100%;
    max-width: none;
  }
}
</style>
