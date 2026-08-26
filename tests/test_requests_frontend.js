const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sourcePath = path.join(__dirname, "..", "static", "requests.js");
const source = fs.readFileSync(sourcePath, "utf8");
const startupIndex = source.indexOf("async function start()");
assert.ok(startupIndex > 0, "request frontend startup marker is missing");

const context = vm.createContext({
  console,
  Intl,
  Date,
  Math,
  Number,
  String,
  Array,
  Object,
  JSON,
  document: { querySelector: () => null, querySelectorAll: () => [] },
});
vm.runInContext(source.slice(0, startupIndex), context, { filename: sourcePath });

context.__service = {
  model_key: "Qwen-Test",
  served_name: "Qwen-Test",
  status: "deploying",
  progress_stage: "starting_vllm",
  progress_percent: 35,
  worker_name: "GPU worker",
  gpu_indices: ["0"],
  container_name: "probe-qwen-test",
  host_port: 18002,
  active_allocations: 0,
  runtime: { status: "deploying", health: "starting" },
};
const deploying = vm.runInContext("modelServiceCard(__service)", context);
assert.match(deploying, /Qwen-Test/);
assert.match(deploying, /正在启动 vLLM/);
assert.match(deploying, /35%/);
assert.match(deploying, /部署中/);

context.__service.status = "running";
context.__service.progress_stage = "running";
context.__service.progress_percent = 100;
context.__service.runtime = {
  status: "running",
  health: "healthy",
  gpu_memory_used_bytes: 8 * 1024 ** 3,
};
const running = vm.runInContext("modelServiceCard(__service)", context);
assert.match(running, /运行正常/);
assert.match(running, /8\.0 GB/);
assert.match(running, /100%/);

console.log("request frontend model deployment rendering checks passed");
