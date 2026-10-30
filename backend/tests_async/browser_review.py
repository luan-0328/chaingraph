"""通过 --browser 显式启用的审批页面测试，需要 Node、前端依赖和 Chrome。"""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import httpx
import pytest

from tests_async.conftest import eventually, execute, query
from tests_async.test_worker_flow import waiting


def frontend_environment(app, processes):
    """将真实 Vite 代理到本例的隔离 API，不占用开发环境固定端口。"""
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    chrome = os.environ.get("CHROME_BINARY") or shutil.which("google-chrome") or shutil.which("chromium")
    assert node and chrome, "浏览器测试需要 Node 与 Chrome，可通过 NODE_BINARY/CHROME_BINARY 指定"
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    assert (frontend / "node_modules/playwright-core/index.mjs").exists(), "请先在 frontend 执行 npm ci"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["VITE_DEV_API_TARGET"] = app.base
    vite = processes.start([node, str(frontend / "node_modules/vite/bin/vite.js"), str(frontend),
                            "--host", "127.0.0.1", "--port", str(port), "--strictPort"], env=env, label="vite")

    def ready():
        assert vite.poll() is None, processes.logs()
        try:
            return httpx.get(base, timeout=0.5, trust_env=False).status_code == 200
        except httpx.TransportError:
            return False

    eventually(ready, description="前端测试服务器启动")
    return node, chrome, frontend, base


@pytest.mark.parametrize("incompatible", [False, True])
def test_browser_review_deadline_version_and_takeover(app, processes, incompatible):
    """从真实登录页进入工单，检查审批提示与按钮，再用界面完成接管。"""
    case, _ = waiting(app, processes)
    if incompatible:
        execute(app.engine, "UPDATE agent_runs SET flow_version=:version WHERE id=:id",
                version="0" * 64, id=app.run(case)["id"])
    node, chrome, frontend, base = frontend_environment(app, processes)
    script = processes.folder / "review-browser.mjs"
    script.write_text("import { chromium } from " + json.dumps((frontend / "node_modules/playwright-core/index.mjs").as_uri()) + ";\n" + """
import assert from 'node:assert/strict';
const [base, chrome, ticket, incompatible] = process.argv.slice(2);
const browser = await chromium.launch({executablePath: chrome, headless: true, args: ['--no-sandbox']});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/login');
  await page.getByPlaceholder('请输入用户名').fill('service01');
  await page.getByPlaceholder('请输入密码').fill('123456');
  await page.getByRole('button', {name: '登 录', exact: true}).click();
  await page.waitForURL(/customer-service/);
  await page.goto(base + '/customer-service/tickets/' + ticket);
  const approval = page.getByRole('button', {name: '人工审批', exact: true});
  await approval.waitFor();
  if (incompatible === 'true') {
    await page.getByText('方案版本与当前系统不兼容，请使用原版本处理或人工接管', {exact: true}).waitFor();
    assert(await approval.isDisabled());
  } else {
    await page.getByText(/待人工审批，截止时间/).waitFor();
    assert(await approval.isEnabled());
  }
  await page.getByRole('button', {name: '人工接管', exact: true}).click();
  await page.getByRole('button', {name: '确定', exact: true}).click();
  await page.getByText('已接管，当前 Agent 方案已停止', {exact: true}).waitFor();
  assert.deepEqual(errors, []);
} finally { await browser.close(); }
""", encoding="utf-8")
    result = subprocess.run([node, str(script), base, chrome, str(case["ticket"]), str(incompatible).lower()],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    assert app.run(case)["status"] == "REJECTED" and app.refund_count(case) == 0
    assert app.detail(case)["assignee_id"] == app.staff.user_id


def test_browser_user_handoff_staff_chat_and_explicit_return(app, processes):
    """两个独立浏览器上下文验证用户转接、客服领取、消息刷新及明确交回。"""
    case = app.scenario()
    username = app.user.ok("GET", "auth/me")["username"]
    node, chrome, frontend, base = frontend_environment(app, processes)
    script = processes.folder / "handoff-browser.mjs"
    script.write_text("import { chromium } from " + json.dumps((frontend / "node_modules/playwright-core/index.mjs").as_uri()) + ";\n" + """
import assert from 'node:assert/strict';
const [base, chrome, ticket, username] = process.argv.slice(2);
const browser = await chromium.launch({executablePath: chrome, headless: true, args: ['--no-sandbox']});
try {
  const user = await (await browser.newContext()).newPage();
  const staff = await (await browser.newContext()).newPage();
  const errors = [];
  for (const page of [user, staff]) page.on('pageerror', error => errors.push(error.message));
  async function login(page, name) {
    await page.goto(base + '/login');
    await page.getByPlaceholder('请输入用户名').fill(name);
    await page.getByPlaceholder('请输入密码').fill('123456');
    await page.getByRole('button', {name: '登 录', exact: true}).click();
    await page.waitForURL(url => !url.pathname.includes('/login'));
  }
  await login(user, username);
  await user.goto(base + '/tickets/' + ticket);
  await user.getByRole('button', {name: '转人工客服', exact: true}).click();
  await user.getByText('已进入人工客服待办队列', {exact: true}).waitFor();
  assert(await user.getByRole('button', {name: /开始处理|让 Agent 处理/}).isDisabled());
  await login(staff, 'service01');
  await staff.goto(base + '/customer-service/tickets/' + ticket);
  await staff.getByRole('button', {name: '领取工单', exact: true}).click();
  await staff.getByText('已领取该工单', {exact: true}).waitFor();
  await staff.locator('textarea').fill('我是人工客服，现在由我核实处理。');
  await staff.getByRole('button', {name: '发送', exact: true}).click();
  await user.getByText('我是人工客服，现在由我核实处理。', {exact: true}).waitFor({timeout: 12000});
  await user.locator('textarea').fill('好的，请帮我核实订单。');
  await user.getByRole('button', {name: '发送', exact: true}).click();
  await staff.getByText('好的，请帮我核实订单。', {exact: true}).waitFor({timeout: 12000});
  await staff.getByRole('button', {name: '交回 Agent', exact: true}).click();
  await staff.getByText('已明确交回 Agent，可发起新的自动处理', {exact: true}).waitFor();
  await user.getByRole('button', {name: '转人工客服', exact: true}).waitFor({timeout: 12000});
  assert(await user.getByRole('button', {name: '开始处理', exact: true}).isEnabled());
  assert.deepEqual(errors, []);
} finally { await browser.close(); }
""", encoding="utf-8")
    result = subprocess.run([node, str(script), base, chrome, str(case["ticket"]), username],
                            capture_output=True, text=True, timeout=75)
    assert result.returncode == 0, result.stderr[-3000:]
    assert app.detail(case)["handling_mode"] == "AGENT" and app.run(case) is None


def test_browser_unread_receipt_and_blank_oss_upload(app, processes):
    case = app.scenario()
    ticket = case['ticket']
    app.user.ok('POST', f'tickets/{ticket}/request-human', json={})
    app.staff.ok('POST', f'customer-service/tickets/{ticket}/assign', json={})
    reply = app.staff.ok('POST', f'customer-service/tickets/{ticket}/messages', json={'content':'人工已回复，请查看消息。'})
    username = app.user.ok('GET', 'auth/me')['username']
    node, chrome, frontend, base = frontend_environment(app, processes)
    script = processes.folder / 'unread-browser.mjs'
    script.write_text('import { chromium } from ' + json.dumps((frontend / 'node_modules/playwright-core/index.mjs').as_uri()) + ';\n' + """
import assert from 'node:assert/strict';
const [base, chrome, ticket, username] = process.argv.slice(2);
const browser = await chromium.launch({executablePath:chrome,headless:true,args:['--no-sandbox']});
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(base + '/login');
  await page.getByPlaceholder('请输入用户名').fill(username);
  await page.getByPlaceholder('请输入密码').fill('123456');
  await page.getByRole('button', {name:'登 录',exact:true}).click();
  await page.waitForURL(url => !url.pathname.includes('/login'));
  const badge = page.locator('.layout__right .el-badge').filter({has:page.getByRole('button',{name:'工单消息',exact:true})});
  await badge.locator('.el-badge__content').filter({hasText:'1'}).waitFor();
  await page.goto(base + '/tickets/' + ticket);
  await page.getByText('人工已回复，请查看消息。',{exact:true}).waitFor();
  await badge.locator('.el-badge__content').waitFor({state:'hidden'});
  await page.locator('input[type=file]').setInputFiles({name:'proof.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64')});
  await page.getByText('图片上传暂不可用，请先使用文字说明',{exact:true}).waitFor();
  assert(await page.getByRole('button',{name:'发送图片',exact:true}).isEnabled());
  assert.deepEqual(errors,[]);
} finally { await browser.close(); }
""", encoding='utf-8')
    result = subprocess.run([node,str(script),base,chrome,str(ticket),username],capture_output=True,text=True,timeout=60)
    assert result.returncode == 0, result.stderr[-3000:]
    assert query(app.engine, 'SELECT last_message_id FROM ticket_reads WHERE ticket_id=:id AND user_id=:user',id=ticket,user=app.user.user_id)[0]['last_message_id'] >= reply['message_id']
