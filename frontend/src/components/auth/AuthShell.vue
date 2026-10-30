<script setup lang="ts">
/**
 * 登录 / 注册共用的外层壳：左侧品牌介绍 + 右侧表单面板。
 *
 * 抽出来的原因：两个页面的表单不同，但版式、品牌区、页脚完全一样；
 * 各自复制一份样式最容易出现"改了登录忘了注册"的不一致。
 */
</script>

<template>
  <div class="auth-shell">
    <aside class="auth-shell__aside">
      <div class="auth-brand">
        <span class="auth-brand__logo">
          <el-icon><Shop /></el-icon>
        </span>
        <span class="auth-brand__text">
          <strong>智能电商</strong>
          <small>客服与售后工单平台</small>
        </span>
      </div>

      <div class="auth-hero">
        <p class="auth-eyebrow">AI TICKET PLATFORM</p>
        <h1 class="auth-hero__title">
          把售后交给流程，<br />
          把判断留给人。
        </h1>
        <p class="auth-hero__lead">
          下单、支付、售后、工单与 Agent 处理在同一条链路上，每一步进度都真实可见。
        </p>

        <ul class="auth-points">
          <li>
            <span class="auth-points__icon"
              ><el-icon><Cpu /></el-icon
            ></span>
            <div>
              <strong>Agent 自动处理</strong>
              <p>查询真实订单与售后数据，按客服规则给出方案</p>
            </div>
          </li>
          <li>
            <span class="auth-points__icon"
              ><el-icon><UserFilled /></el-icon
            ></span>
            <div>
              <strong>人工审批兜底</strong>
              <p>涉及退款等敏感操作，由客服确认后再继续执行</p>
            </div>
          </li>
          <li>
            <span class="auth-points__icon"
              ><el-icon><DataLine /></el-icon
            ></span>
            <div>
              <strong>进度全程可追溯</strong>
              <p>每个业务事件都落在时间线上，断开重连也不丢</p>
            </div>
          </li>
        </ul>

        <div class="auth-flow" aria-hidden="true">
          <span class="auth-flow__step">接收问题</span>
          <span class="auth-flow__line"></span>
          <span class="auth-flow__step">Agent 分析</span>
          <span class="auth-flow__line"></span>
          <span class="auth-flow__step auth-flow__step--accent">人工审批</span>
        </div>
      </div>

      <div class="auth-foot">v2.0 · 智能电商客服与售后工单平台</div>
    </aside>

    <main class="auth-shell__main">
      <div class="auth-shell__panel">
        <slot />
      </div>
      <p class="auth-shell__copyright">© 2026 智能电商客服平台 · 内部演示环境</p>
    </main>
  </div>
</template>

<style scoped>
.auth-shell {
  display: flex;
  min-height: 100vh;
  background: #f7f9fd;
}

.auth-shell__aside {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 52%;
  max-width: 720px;
  min-height: 100vh;
  padding: 44px 56px;
  background: #101a33;
  color: #fff;
  overflow: hidden;
}

/* 两个细圆环做背景层次：是描边不是渐变，符合 v2.md §71 的"不要渐变" */
.auth-shell__aside::before,
.auth-shell__aside::after {
  content: '';
  position: absolute;
  border: 1px solid rgb(255 255 255 / 7%);
  border-radius: 50%;
  pointer-events: none;
}

.auth-shell__aside::before {
  width: 520px;
  height: 520px;
  right: -220px;
  top: -180px;
}

.auth-shell__aside::after {
  width: 360px;
  height: 360px;
  right: -80px;
  bottom: -120px;
}

.auth-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  position: relative;
  z-index: 1;
}

.auth-brand__logo {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 11px;
  background: var(--brand);
  color: #fff;
  font-size: 18px;
  box-shadow: 0 8px 20px -6px rgb(59 108 240 / 65%);
}

.auth-brand__text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
}

.auth-brand__text strong {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: 0.2px;
}

.auth-brand__text small {
  margin-top: 3px;
  color: rgb(255 255 255 / 55%);
  font-size: 11px;
  letter-spacing: 0.3px;
}

.auth-hero {
  position: relative;
  z-index: 1;
  margin-top: auto;
  margin-bottom: auto;
  padding: 48px 0;
}

.auth-eyebrow {
  margin: 0 0 16px;
  color: #8fb0ff;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 2.4px;
}

.auth-hero__title {
  margin: 0;
  font-size: 34px;
  line-height: 1.35;
  font-weight: 650;
  letter-spacing: -0.5px;
}

.auth-hero__lead {
  max-width: 420px;
  margin: 16px 0 0;
  color: rgb(255 255 255 / 62%);
  font-size: 13.5px;
  line-height: 1.8;
}

.auth-points {
  display: flex;
  flex-direction: column;
  gap: 18px;
  margin: 36px 0 0;
  padding: 0;
  list-style: none;
}

.auth-points li {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.auth-points__icon {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: rgb(255 255 255 / 8%);
  border: 1px solid rgb(255 255 255 / 10%);
  color: #a9c1ff;
  font-size: 16px;
  flex: none;
}

.auth-points strong {
  display: block;
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
}

.auth-points p {
  margin: 3px 0 0;
  color: rgb(255 255 255 / 52%);
  font-size: 12.5px;
  line-height: 1.6;
}

.auth-flow {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 36px;
}

.auth-flow__step {
  padding: 6px 12px;
  border: 1px solid rgb(255 255 255 / 12%);
  border-radius: var(--radius-pill);
  background: rgb(255 255 255 / 5%);
  color: rgb(255 255 255 / 72%);
  font-size: 11.5px;
  white-space: nowrap;
}

.auth-flow__step--accent {
  border-color: rgb(143 176 255 / 45%);
  background: rgb(59 108 240 / 22%);
  color: #c9d8ff;
}

.auth-flow__line {
  width: 22px;
  height: 1px;
  background: rgb(255 255 255 / 16%);
  flex: none;
}

.auth-foot {
  position: relative;
  z-index: 1;
  color: rgb(255 255 255 / 35%);
  font-size: 11px;
  letter-spacing: 0.4px;
}

.auth-shell__main {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  min-width: 0;
  padding: 40px 24px;
}

.auth-shell__panel {
  width: 100%;
  max-width: 430px;
  padding: 32px;
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  box-shadow: 0 20px 48px -24px rgb(20 30 55 / 18%);
}

.auth-shell__copyright {
  margin: 18px 0 0;
  color: var(--text-secondary);
  font-size: 11.5px;
}

@media (max-width: 980px) {
  .auth-shell__aside {
    display: none;
  }

  .auth-shell__main {
    background: #f7f9fd;
  }
}

@media (max-width: 520px) {
  .auth-shell__panel {
    padding: 24px 20px;
    border-radius: var(--radius-lg);
  }

  .auth-shell__main {
    padding: 24px 16px;
    justify-content: flex-start;
  }
}
</style>
