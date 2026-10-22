/**
 * 阶段 14 验收的最终验证：对 `docker compose up` 起来的**真实全栈**走完整流程。
 *
 * 与上一次「隔离容器验证」的区别：这次**不手动调 Worker 函数**，
 * 而是让 API 派发的 Celery 任务由 logagent-worker 容器自己消费 ——
 * 这才是 README 里描述的真实使用路径。
 */

const fs = require("fs");
const http = require("http");

const BASE = { host: "127.0.0.1", port: 8000 };
let TOKEN = null;

function request(method, path, { json, raw, contentType } = {}) {
  return new Promise((resolve, reject) => {
    const headers = {};
    if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;
    let body = null;
    if (json !== undefined) {
      body = Buffer.from(JSON.stringify(json), "utf8");
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = body.length;
    } else if (raw !== undefined) {
      body = raw;
      headers["Content-Type"] = contentType || "application/octet-stream";
      headers["Content-Length"] = body.length;
    }
    const req = http.request({ ...BASE, method, path, headers, timeout: 120000 }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let parsed = null;
        try { parsed = JSON.parse(text); } catch { parsed = text; }
        resolve({ status: res.statusCode, body: parsed, text });
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
    if (body) req.write(body);
    req.end();
  });
}

function multipart(fields) {
  const boundary = "----logagentfinal" + Date.now();
  const parts = [];
  for (const [name, value] of Object.entries(fields)) {
    if (value && value.file) {
      parts.push(Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="${name}"; filename="${value.filename}"\r\n` +
        `Content-Type: ${value.type || "text/plain"}\r\n\r\n`, "utf8"));
      parts.push(value.file);
      parts.push(Buffer.from("\r\n", "utf8"));
    } else {
      parts.push(Buffer.from(
        `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`, "utf8"));
    }
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`, "utf8"));
  return { body: Buffer.concat(parts), contentType: `multipart/form-data; boundary=${boundary}` };
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  PASS" : "  FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const email = `final-${Date.now()}@example.com`;

  console.log("① 健康检查（经真实 HTTP）");
  let r = await request("GET", "/healthz");
  check("/healthz 200", r.status === 200, `HTTP ${r.status}`);
  r = await request("GET", "/healthz/deps");
  check("/healthz/deps 200 且依赖可用", r.status === 200 && r.body.checks.postgres.ok && r.body.checks.redis.ok,
        `pg=${r.body.checks?.postgres?.server_version} redis=${r.body.checks?.redis?.version}`);

  console.log("② 注册 / 登录");
  r = await request("POST", "/api/register", { json: { email, password: "strong-password" } });
  check("注册 200", r.status === 200, `HTTP ${r.status}`);
  TOKEN = r.body.access_token;

  console.log("③ 建项目 + 上传真实崩溃日志");
  r = await request("POST", "/api/projects", { json: { name: `final-${Date.now()}`, budget_total: 10 } });
  check("建项目 201", r.status === 201, `HTTP ${r.status}`);
  const projectId = r.body.id;

  const logBytes = fs.readFileSync("tests/datasets/crash/system.log");
  const form = multipart({ file: { file: logBytes, filename: "crash.log" }, fmt: "txt" });
  r = await request("POST", `/api/projects/${projectId}/upload`, { raw: form.body, contentType: form.contentType });
  check("上传返回 202", r.status === 202, `HTTP ${r.status}`);
  const uploadJobId = r.body.job_id;
  for (let attempt = 0; attempt < 300; attempt++) {
    r = await request("GET", `/api/projects/${projectId}/uploads/${uploadJobId}`);
    if (['completed', 'failed'].includes(r.body.status)) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  check("异步上传解析完成且事件入库", r.status === 200 && r.body.status === "completed" && r.body.events_persisted > 0,
        `persisted=${r.body.events_persisted} parsed=${r.body.parse?.parsed} bad=${r.body.parse?.bad_lines}`);
  const sourceId = r.body.data_source_id;

  console.log("④ 发起分析（交给 Celery，不手动执行）");
  const t0 = Date.now();
  r = await request("POST", `/api/projects/${projectId}/analysis-runs`, { json: { data_source_id: sourceId } });
  const elapsed = Date.now() - t0;
  check("返回 202 且 queued", r.status === 202 && r.body.status === "queued", `HTTP ${r.status} status=${r.body.status}`);
  check("未阻塞（<5s）", elapsed < 5000, `${elapsed}ms`);
  const runId = r.body.run_id;

  console.log("⑤ 轮询等待 Worker 消费并执行完（最多 90s）");
  let status = "queued";
  const terminal = ["completed", "partial_success", "failed", "timeout", "cancelled"];
  let last = null;
  for (let i = 0; i < 45; i++) {
    await sleep(2000);
    r = await request("GET", `/api/runs/${runId}?project_id=${projectId}`);
    last = r.body;
    status = r.body.status;
    if (terminal.includes(status)) break;
  }
  check("Worker 真的消费了任务（离开 queued）", status !== "queued", `status=${status}`);
  check("Run 进入终态", terminal.includes(status), `status=${status}`);
  console.log(`      phase=${last?.current_phase} tokens=${last?.tokens_input}/${last?.tokens_output} cost=¥${last?.cost_actual}`);
  console.log(`      stop_reason=${last?.run_metadata?.stop_reason}`);

  console.log("⑥ Run 详情复述");
  r = await request("GET", `/api/runs/${runId}/detail?project_id=${projectId}`);
  check("详情可读", r.status === 200, `HTTP ${r.status}`);
  const narrative = r.body.narrative || "";
  check("复述含阶段与花费", narrative.includes("模型") || narrative.includes("阶段") || narrative.includes("¥"),
        narrative.split("\n").slice(0, 2).join(" | ").slice(0, 120));

  console.log("⑦ 结论与证据");
  r = await request("GET", `/api/runs/${runId}/insights?project_id=${projectId}`);
  check("结论列表可读", r.status === 200, `HTTP ${r.status} 共 ${Array.isArray(r.body) ? r.body.length : "?"} 条`);

  console.log("⑧ 跨项目隔离");
  const saved = TOKEN;
  r = await request("POST", "/api/register", { json: { email: `intruder2-${Date.now()}@example.com`, password: "strong-password" } });
  TOKEN = r.body.access_token;
  r = await request("GET", `/api/projects/${projectId}/data-sources`);
  check("他人访问项目 → 404", r.status === 404, `HTTP ${r.status}`);
  r = await request("GET", `/api/runs/${runId}?project_id=${projectId}`);
  check("他人读 Run → 404", r.status === 404, `HTTP ${r.status}`);
  TOKEN = saved;

  console.log("⑨ 页面与文档");
  r = await request("GET", "/login");
  check("登录页可渲染", r.status === 200 && String(r.text).includes("<!DOCTYPE html>"), `HTTP ${r.status}`);
  r = await request("GET", "/docs");
  check("/docs 可访问", r.status === 200, `HTTP ${r.status}`);

  const failed = results.filter((x) => !x.ok);
  console.log("");
  console.log(`===== ${results.length - failed.length}/${results.length} 项通过 =====`);
  if (failed.length) {
    console.log("失败项：");
    for (const f of failed) console.log(`  - ${f.name} (${f.detail})`);
    process.exit(1);
  }
}

main().catch((e) => { console.error("ERROR:", e.message); process.exit(2); });
