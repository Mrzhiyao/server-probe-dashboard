const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appPath = path.join(__dirname, "..", "static", "app.js");
const source = fs.readFileSync(appPath, "utf8");
const startupIndex = source.indexOf('els.refreshButton.addEventListener("click"');
assert.ok(startupIndex > 0, "frontend startup marker is missing");

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
  localStorage: { getItem: () => null, setItem: () => {} },
  document: { querySelector: () => null, querySelectorAll: () => [] },
});
vm.runInContext(source.slice(0, startupIndex), context, { filename: appPath });

const result = {
  id: "edge-24",
  name: "192.168.2.24",
  host: "192.168.2.24",
  group: "边缘设备",
  status: "online",
  alerts: [],
  metrics: {
    docker: {
      available: true,
      accessible: true,
      version: "29.4.1",
      summary: { container_count: 1, running_count: 1, unhealthy_count: 0, vllm_running_count: 1, image_count: 1 },
      disk_usage: { images: { size_bytes: 20000000000, reclaimable_bytes: 1000000000 } },
      images: [{ repository: "vllm/vllm-openai", tag: "latest", size_bytes: 20000000000, vllm: true }],
      containers: [
        {
          id: "abcdef123456",
          name: "model-api",
          image: "vllm/vllm-openai:latest",
          state: "running",
          running: true,
          cpu_percent: 12,
          memory_percent: 25,
          memory_used_bytes: 4000000000,
          ports: ["0.0.0.0:18223->18223/tcp"],
          gpu_indices: ["0"],
          gpu_memory_used_bytes: 8000000000,
          vllm: {
            service: true,
            model: "Qwen3-VL-8B-Instruct",
            version: "0.19.0",
            probe: { status: "healthy", endpoint: "127.0.0.1:18223", latency_ms: 3 },
          },
        },
      ],
    },
    storage: {
      smartctl_available: true,
      summary: { mount_count: 2, mounted_count: 2, mount_issue_count: 0, device_count: 1, smart_issue_count: 0 },
      mounts: [
        {
          mount: "/nas",
          source: "//p8.example/very-long-share-name/team-data",
          fstype: "cifs",
          kind: "network",
          status: "mounted",
          expected: true,
          automount: true,
          percent: null,
          latency_ms: 18,
        },
        {
          mount: "/",
          source: "/dev/nvme0n1p1",
          fstype: "ext4",
          kind: "local",
          status: "mounted",
          expected: true,
          percent: 42.5,
          total_bytes: 1000000000000,
          used_bytes: 425000000000,
          inode_percent: 8,
          io: { read_bytes_per_second: 1024, write_bytes_per_second: 2048 },
        },
      ],
      devices: [
        {
          name: "nvme0n1",
          model: "Example NVMe",
          transport: "nvme",
          size_bytes: 1000000000000,
          smart: { available: true, health: "passed", temperature_c: 41, percentage_used: 2, power_on_hours: 2400 },
          io: { read_bytes_per_second: 1024, write_bytes_per_second: 2048 },
        },
      ],
    },
  },
};

context.__result = result;
const html = vm.runInContext("renderStorageHost(__result)", context);
assert.match(html, /192\.168\.2\.24/);
assert.match(html, /\/nas/);
assert.match(html, /very-long-share-name/);
assert.match(html, /Example NVMe/);
assert.match(html, /存储正常/);

const containerHtml = vm.runInContext("renderContainerHost(__result)", context);
assert.match(containerHtml, /model-api/);
assert.match(containerHtml, /Qwen3-VL-8B-Instruct/);
assert.match(containerHtml, /接口正常/);
assert.match(containerHtml, /Docker 29\.4\.1/);

context.__alert = {
  kind: "mount",
  path: "/nas",
  message: "automatic mount placeholder is active but the real filesystem is not mounted",
};
const alertText = vm.runInContext("alertText(__alert)", context);
assert.equal(alertText, "/nas · 只有自动挂载占位，真实文件系统未挂载");

context.__containerAlert = { kind: "vllm", container: "model-api", model: "Qwen", message: "ignored" };
assert.equal(vm.runInContext("alertText(__containerAlert)", context), "model-api · Qwen 接口不可用");

context.__persistentHistory = {
  "edge-24": [
    { time: "2026-08-23T10:00:00Z", status: "online", cpu: 10 },
    { time: "2026-08-23T11:00:00Z", status: "online", cpu: 20 },
  ],
};
const historyCpu = vm.runInContext(
  'state.snapshot = { history: { "edge-24": [{ cpu: 1 }] } }; state.historyOverride = __persistentHistory; historySamples({ id: "edge-24" }).map((sample) => sample.cpu)',
  context
);
assert.deepEqual(Array.from(historyCpu), [10, 20]);

console.log("frontend storage, history, and container rendering checks passed");
