/**
 * 临时 E2E：无头浏览器真机走一遍 V2 前端（验证完即删）。
 *
 * 覆盖三条角色链路 + 关键业务动作，并收集控制台错误/失败请求。
 * 截图输出到 D:\chaingraph\_shots_v2\
 */

import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = 'http://127.0.0.1:5173'
const SHOTS = 'D:\\chaingraph\\_shots_v2'
mkdirSync(SHOTS, { recursive: true })

const errors = []
const failed = []

function step(title) {
  console.log(`\n=== ${title} ===`)
}

const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' })
const page = await context.newPage()

page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(msg.text().slice(0, 160))
})
page.on('pageerror', (e) => errors.push(`[pageerror] ${e.message.slice(0, 160)}`))
page.on('requestfailed', (r) => failed.push(`${r.method()} ${r.url()} — ${r.failure()?.errorText}`))

async function login(username, password) {
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await page.locator('input').first().fill(username)
  await page.locator('input[type="password"]').first().fill(password)
  await page.getByRole('button', { name: /登\s*录/ }).click()
  await page.waitForTimeout(2500)
}

try {
  // ================================================================
  step('① 守卫：未登录访问 /tickets 应跳登录页')
  await page.goto(`${BASE}/tickets`, { waitUntil: 'networkidle' })
  await page.waitForURL(/\/login/, { timeout: 10_000 })
  console.log(`  URL: ${page.url()}`)
  await page.screenshot({ path: `${SHOTS}\\01-login.png` })

  // ================================================================
  step('② 用户端：登录 alice → 商品列表')
  await login('alice', '123456')
  console.log(`  落地页: ${page.url()}`)
  await page.waitForSelector('text=商品', { timeout: 15_000 })
  const productCount = await page.locator('.product-card').count()
  console.log(`  商品卡片数: ${productCount}`)
  await page.screenshot({ path: `${SHOTS}\\02-products.png` })

  // 加入购物车（挑一个**可购买**的卡片：种子数据里有库存 0 的商品，按钮是禁用的）
  // 必须按 class 直接定位真正的 <button>：商品卡片本身也带 role="button"（整卡可点进详情），
  // 用 getByRole('button', { name: '加入购物车' }) 会把"卡片 div"和"按钮"一起匹配到，
  // 第一个命中的是卡片 → 点下去是进详情页，购物车永远为空。
  const addButtons = page.locator('button.product-card__add')
  const total = await addButtons.count()
  let added = false
  for (let i = 0; i < total; i += 1) {
    const btn = addButtons.nth(i)
    if (await btn.isDisabled()) continue
    await btn.click()
    await page.waitForTimeout(2000)
    added = true
    console.log(`  已点击第 ${i + 1} 个卡片的加入购物车（共 ${total} 个）`)
    break
  }
  if (!added) console.log('  ⚠️ 所有加购按钮都被禁用（库存为 0）')

  // ================================================================
  step('③ 购物车 → 结算下单')
  await page.goto(`${BASE}/cart`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const cartRows = await page.locator('.el-table__body tr').count()
  console.log(`  购物车行数: ${cartRows}`)
  await page.screenshot({ path: `${SHOTS}\\03-cart.png` })

  const checkout = page.getByRole('button', { name: /结算/ }).first()
  if (await checkout.count()) {
    await checkout.click()
    await page.waitForTimeout(3000)
    console.log(`  下单后 URL: ${page.url()}`)
    await page.screenshot({ path: `${SHOTS}\\04-order-detail.png`, fullPage: true })
  } else {
    console.log('  ⚠️ 未找到结算按钮')
  }

  // ================================================================
  step('④ 订单列表 + 支付')
  await page.goto(`${BASE}/orders`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const orderRows = await page.locator('.el-table__body tr').count()
  console.log(`  订单行数: ${orderRows}`)
  await page.screenshot({ path: `${SHOTS}\\05-orders.png` })

  const payBtn = page.getByRole('button', { name: '去支付' }).first()
  if (await payBtn.count()) {
    await payBtn.click()
    await page.waitForTimeout(1000)
    // 支付是二次确认弹窗（ElMessageBox）：不点"确认支付"订单不会真的付掉，
    // 后面的售后/退款链路就完全跑不起来 —— 这里踩过坑。
    const confirmPay = page.locator('.el-message-box__btns .el-button--primary')
    if (await confirmPay.count()) {
      await confirmPay.click()
      await page.waitForTimeout(2500)
      console.log('  已在确认弹窗里点"确认支付"')
    } else {
      console.log('  ⚠️ 没等到支付确认弹窗')
    }
  } else {
    console.log('  ⚠️ 未找到"去支付"按钮（可能订单已支付或按钮在详情页）')
  }
  const firstRow = await page
    .locator('.el-table__body tr')
    .first()
    .innerText()
    .catch(() => '')
  console.log(`  支付后订单首行: ${firstRow.replace(/\s+/g, ' ').slice(0, 70)}`)

  // ================================================================
  step('④b 售后：申请售后 → 申请退款（自动退款分支）')
  const applyBtn = page.getByRole('button', { name: '申请售后' }).first()
  if (await applyBtn.count()) {
    await applyBtn.click()
    await page.waitForTimeout(1500) // 详情页带 ?afterSale=1 会自动弹出申请弹窗
    await page.locator('.el-dialog textarea').first().fill('测试脚本：不想要了，申请退款')
    await page.getByRole('button', { name: '提交申请' }).click()
    await page.waitForTimeout(2000)
    console.log('  已提交售后申请')
  } else {
    console.log('  ⚠️ 没找到"申请售后"按钮')
  }

  await page.goto(`${BASE}/after-sales`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1500)
  const saleRows = await page.locator('.el-table__body tr').count()
  console.log(`  售后单行数: ${saleRows}`)
  const refundBtn = page.getByRole('button', { name: '申请退款' }).first()
  if (await refundBtn.count()) {
    await refundBtn.click()
    await page.waitForTimeout(1000)
    await page.locator('.el-message-box__btns .el-button--primary').click()
    await page.waitForTimeout(3000)
    const afterSaleText = await page.locator('body').innerText()
    console.log(`  申请退款后含"已退款": ${afterSaleText.includes('已退款')}`)
    console.log(`  申请退款后含"人工": ${afterSaleText.includes('人工')}`)
    await page.screenshot({ path: `${SHOTS}\\04b-after-sale.png`, fullPage: true })
  } else {
    console.log('  ⚠️ 没找到"申请退款"按钮')
  }

  // ================================================================
  step('⑤ 工单：提交问题 → 详情 → 让 Agent 处理（SSE 实时进度）')
  await page.goto(`${BASE}/tickets/create`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1000)
  await page.locator('textarea').first().fill('我的订单一直没发货，我想退款')
  await page.getByRole('button', { name: '提交工单' }).click()
  await page.waitForTimeout(3000)
  console.log(`  创建后 URL: ${page.url()}`)
  await page.screenshot({ path: `${SHOTS}\\06-ticket-detail.png`, fullPage: true })

  const processBtn = page.getByRole('button', { name: /处理/ }).first()
  if (await processBtn.count()) {
    console.log('  点击"让 Agent 处理"，等待真实 LLM 执行（最多 120 秒）…')
    await processBtn.click()
    await page.waitForTimeout(8000)
    await page.screenshot({ path: `${SHOTS}\\07-agent-running.png`, fullPage: true })
    // 等结果（Agent 事件时间线里出现"处理完成"或状态变为已解决）
    await page
      .waitForSelector('text=处理完成', { timeout: 120_000 })
      .then(() => console.log('  ✓ 页面出现"处理完成"'))
      .catch(() => console.log('  ⚠️ 未在 120 秒内看到"处理完成"'))
    await page.waitForTimeout(1500)
    await page.screenshot({ path: `${SHOTS}\\08-agent-result.png`, fullPage: true })
    const bodyText = await page.locator('body').innerText()
    for (const keyword of ['Agent', '参考知识', '处理方案', '退款']) {
      console.log(`  页面含"${keyword}": ${bodyText.includes(keyword)}`)
    }
  } else {
    console.log('  ⚠️ 未找到处理按钮')
  }

  // ================================================================
  step('⑥ 客服端：登录 service01 → 工作台')
  await page.evaluate(() => localStorage.clear())
  await login('service01', '123456')
  console.log(`  落地页: ${page.url()}`)
  await page.waitForSelector('text=工作台', { timeout: 15_000 })
  await page.waitForTimeout(2000)
  const cards = await page.locator('.el-card, .stat-card, .page-card').count()
  console.log(`  概览卡片/区块数: ${cards}`)
  await page.screenshot({ path: `${SHOTS}\\09-cs-workbench.png`, fullPage: true })

  step('⑦ 客服端：工单处理')
  await page.goto(`${BASE}/customer-service/tickets`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)
  const csRows = await page.locator('.el-table__body tr').count()
  console.log(`  工单行数: ${csRows}`)
  await page.screenshot({ path: `${SHOTS}\\10-cs-tickets.png` })

  step('⑧ 管理端：登录 admin → 商品管理 / 知识库')
  await page.evaluate(() => localStorage.clear())
  await login('admin', 'admin123')
  console.log(`  落地页: ${page.url()}`)
  await page.waitForTimeout(2000)
  await page.screenshot({ path: `${SHOTS}\\11-admin-products.png` })
  await page.goto(`${BASE}/admin/knowledge`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)
  const kbRows = await page.locator('.el-table__body tr').count()
  console.log(`  知识文档行数: ${kbRows}`)
  await page.screenshot({ path: `${SHOTS}\\12-admin-knowledge.png`, fullPage: true })
} catch (error) {
  console.log(`\n❌ 流程中断: ${error.message}`)
  await page.screenshot({ path: `${SHOTS}\\99-error.png`, fullPage: true }).catch(() => {})
} finally {
  console.log('\n=== 浏览器层面问题 ===')
  console.log(`  控制台错误 ${errors.length} 条`)
  ;[...new Set(errors)].slice(0, 8).forEach((e) => console.log(`    ! ${e}`))
  console.log(`  失败请求 ${failed.length} 条`)
  ;[...new Set(failed)].slice(0, 8).forEach((e) => console.log(`    ! ${e}`))
  await browser.close()
}
