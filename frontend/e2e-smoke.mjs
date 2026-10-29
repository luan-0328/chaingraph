/**
 * 临时 E2E 脚本：用无头浏览器把前端真实点一遍，并截图（验证完即删）。
 *
 * 同时收集：控制台错误、页面异常、失败请求 —— 这些在"只看构建成功"时是发现不了的。
 */

import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:5173'
const SHOTS = 'D:\\chaingraph\\_shots'
const VIEWPORT = { width: 1440, height: 900 }

mkdirSync(SHOTS, { recursive: true })

const consoleErrors = []
const pageErrors = []
const failedRequests = []

async function launch() {
  const attempts = [
    { channel: 'chrome' },
    { channel: 'msedge' },
    { executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe' },
    { executablePath: 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' },
  ]
  let lastError
  for (const options of attempts) {
    try {
      const browser = await chromium.launch({ headless: true, ...options })
      console.log(`  浏览器启动成功：${JSON.stringify(options)}`)
      return browser
    } catch (error) {
      lastError = error
    }
  }
  throw lastError
}

function step(title) {
  console.log(`\n=== ${title} ===`)
}

const browser = await launch()
const context = await browser.newContext({ viewport: VIEWPORT, locale: 'zh-CN' })
const page = await context.newPage()

page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrors.push(msg.text())
})
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('requestfailed', (request) => {
  failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText}`)
})

try {
  // ---------------------------------------------------------------- 1. 路由守卫
  step('① 未登录访问 /tickets → 应被守卫踢到 /login')
  await page.goto(`${BASE}/tickets`, { waitUntil: 'networkidle' })
  await page.waitForURL(/\/login/, { timeout: 15_000 })
  console.log(`  当前地址：${page.url()}`)
  console.log(`  登录卡片标题：${await page.locator('.login-card__title').innerText()}`)
  await page.screenshot({ path: `${SHOTS}\\01-login.png` })

  // ---------------------------------------------------------------- 2. 登录
  step('② 一键填入演示账号 → 登录')
  await page.getByRole('button', { name: '一键填入' }).click()
  console.log(`  用户名框：${await page.locator('input[placeholder="请输入用户名"]').inputValue()}`)
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await page.waitForURL(/\/tickets$/, { timeout: 20_000 })
  await page.waitForSelector('text=工单管理', { timeout: 20_000 })
  console.log(`  登录后地址：${page.url()}`)
  console.log(`  右上角用户：${await page.locator('.app-header__username').innerText()}`)
  const rowCount = await page.locator('.el-table__body tr').count()
  console.log(`  列表行数：${rowCount}`)
  await page.screenshot({ path: `${SHOTS}\\02-ticket-list.png` })

  // ---------------------------------------------------------------- 3. 创建工单
  step('③ 新建工单 → 提交')
  await page.getByRole('button', { name: /新建工单/ }).click()
  await page.waitForURL(/\/tickets\/create$/, { timeout: 20_000 })
  await page
    .locator('textarea')
    .fill('我的订单10002我要退款')
  await page.getByRole('button', { name: /提交工单/ }).click()
  await page.waitForURL(/\/tickets\/\d+$/, { timeout: 30_000 })
  const ticketUrl = page.url()
  console.log(`  创建后跳转到：${ticketUrl}`)
  await page.waitForSelector('text=Agent 处理结果', { timeout: 20_000 })
  await page.screenshot({ path: `${SHOTS}\\03-detail-before-process.png`, fullPage: true })

  // ---------------------------------------------------------------- 4. Agent 处理
  step('④ 点击「开始处理」→ 等待 Agent 跑完（真实 LLM，约 5~15 秒）')
  const started = Date.now()
  await page.getByRole('button', { name: /开始处理/ }).click()

  // 处理中：按钮应该禁用 + 文案变成"处理中…"
  await page.waitForTimeout(2500)
  const processingButton = page.getByRole('button', { name: /处理中/ })
  const disabledDuringProcess = await processingButton.isDisabled().catch(() => false)
  console.log(`  处理中按钮文案：处理中… / 是否禁用：${disabledDuringProcess}`)
  await page.screenshot({ path: `${SHOTS}\\04-processing.png` })

  // 等到"去确认"出现 = 工单进入待人工确认
  await page.waitForSelector('text=需要人工确认', { timeout: 120_000 })
  console.log(`  Agent 处理耗时：${((Date.now() - started) / 1000).toFixed(1)}s`)
  console.log(`  工单状态标签：${await page.locator('.action-bar .el-tag').innerText()}`)
  const timelineItems = await page.locator('.el-timeline-item').allInnerTexts()
  console.log(`  时间线步骤（${timelineItems.length}）：`)
  timelineItems.forEach((text) => console.log(`    · ${text.replace(/\n/g, ' ')}`))
  console.log(`  退款记录行数：${await page.locator('.el-table__body tr').count()}`)
  await page.screenshot({ path: `${SHOTS}\\05-detail-waiting-approval.png`, fullPage: true })

  // ---------------------------------------------------------------- 5. 人工审批弹窗
  step('⑤ 打开人工确认弹窗')
  await page.getByRole('button', { name: '去确认' }).click()
  await page.waitForSelector('.el-dialog', { timeout: 20_000 })
  console.log(`  弹窗标题：${await page.locator('.el-dialog__title').innerText()}`)
  console.log(`  待审核退款行数：${await page.locator('.el-dialog .el-table__body tr').count()}`)
  await page.screenshot({ path: `${SHOTS}\\06-approval-dialog.png` })

  // ---------------------------------------------------------------- 6. 确认处理
  step('⑥ 填写审批意见 → 确认处理')
  await page.locator('.el-dialog textarea').fill('确认退款')
  await page.getByRole('button', { name: '确认处理' }).click()
  await page.waitForSelector('.el-dialog', { state: 'hidden', timeout: 30_000 })
  await page.waitForTimeout(1200)
  console.log(`  审批后状态标签：${await page.locator('.action-bar .el-tag').innerText()}`)
  console.log(`  人工处理意见：${await page.locator('text=人工处理意见').first().isVisible()}`)
  await page.screenshot({ path: `${SHOTS}\\07-after-approve.png`, fullPage: true })

  // ---------------------------------------------------------------- 7. 搜索/筛选
  step('⑦ 回列表：搜索 + 状态筛选')
  await page.goto(`${BASE}/tickets`, { waitUntil: 'networkidle' })
  await page.locator('input[placeholder="搜索工单编号 / 标题 / 内容"]').fill('10002')
  await page.getByRole('button', { name: '查询' }).click()
  await page.waitForTimeout(1500)
  console.log(`  搜索 "10002" 命中行数：${await page.locator('.el-table__body tr').count()}`)
  await page.locator('input[placeholder="搜索工单编号 / 标题 / 内容"]').fill('')
  await page.getByRole('button', { name: '查询' }).click()
  await page.waitForTimeout(1200)
  console.log(`  分页器文案：${await page.locator('.el-pagination').innerText()}`)
  await page.screenshot({ path: `${SHOTS}\\08-list-after.png` })

  // ---------------------------------------------------------------- 8. 刷新恢复 + 登出
  step('⑧ 刷新详情页（应保持登录）+ 退出登录')
  await page.goto(ticketUrl, { waitUntil: 'networkidle' })
  await page.waitForSelector('text=Agent 处理结果', { timeout: 20_000 })
  console.log(`  刷新后仍在详情页：${page.url()}`)
  console.log(`  刷新后状态标签：${await page.locator('.action-bar .el-tag').innerText()}`)

  await page.locator('.app-header__user').click()
  await page.getByRole('menuitem', { name: '退出登录' }).click()
  await page.getByRole('button', { name: '退出' }).click()
  await page.waitForURL(/\/login$/, { timeout: 20_000 })
  console.log(`  退出后地址：${page.url()}`)
  const tokenAfterLogout = await page.evaluate(() => localStorage.getItem('ticket_agent_token'))
  console.log(`  退出后 localStorage 里的 token：${tokenAfterLogout}`)
} catch (error) {
  console.log(`\n❌ 流程中断：${error.message}`)
  await page.screenshot({ path: `${SHOTS}\\99-error.png`, fullPage: true }).catch(() => {})
} finally {
  console.log('\n=== 浏览器层面的问题 ===')
  console.log(`  控制台错误 ${consoleErrors.length} 条`)
  consoleErrors.slice(0, 10).forEach((text) => console.log(`    ! ${text.slice(0, 200)}`))
  console.log(`  页面异常 ${pageErrors.length} 条`)
  pageErrors.slice(0, 10).forEach((text) => console.log(`    ! ${text.slice(0, 200)}`))
  console.log(`  失败请求 ${failedRequests.length} 条`)
  failedRequests.slice(0, 10).forEach((text) => console.log(`    ! ${text.slice(0, 200)}`))

  await browser.close()
}
