<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { setUnauthorizedHandler } from '@/api'
import AppLayout from '@/layouts/AppLayout.vue'

const route = useRoute()
const router = useRouter()

// 登录页 / 403 等公开页不套后台框架
const isBare = computed(() => route.meta.public || route.name === 'forbidden')

onMounted(() => {
  // R-FE2-10：token 失效时清本地状态并跳登录页
  setUnauthorizedHandler(() => {
    if (route.name !== 'login') {
      router.replace({ path: '/login', query: { redirect: route.fullPath } })
    }
  })
})
</script>

<template>
  <router-view v-if="isBare" />
  <AppLayout v-else />
</template>
