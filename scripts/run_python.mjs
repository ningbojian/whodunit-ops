#!/usr/bin/env node
/**
 * 跨平台 Python 启动器：npm scripts 不依赖 shell 的 PATH 差异。
 *
 * Windows 上 `python` 可能指向应用商店的占位程序，直接调用只会弹出商店。
 * 这里先探测真实解释器，再原样转发参数；可用 `PYTHON` 显式指定。
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

function candidates() {
  const values = [];
  if (process.env.PYTHON) values.push(process.env.PYTHON);

  if (process.platform === "win32") {
    const home = homedir();
    const programData = process.env.ProgramData || "C:\\ProgramData";
    const localAppData = process.env.LOCALAPPDATA || "";
    values.push(
      path.join(home, "anaconda3", "python.exe"),
      path.join(home, "miniconda3", "python.exe"),
      path.join(programData, "anaconda3", "python.exe"),
      path.join(programData, "miniconda3", "python.exe"),
    );
    if (localAppData) {
      values.push(path.join(localAppData, "Programs", "Python", "python.exe"));
    }
    values.push("python", "python3");
  } else {
    values.push("python3", "python");
  }
  return [...new Set(values.filter(Boolean))];
}

function probe(command) {
  if (command.includes("\\") || command.includes("/")) {
    if (!existsSync(command)) return false;
  }
  const result = spawnSync(command, ["-c", "import sys; print(sys.version_info[0])"], {
    stdio: "ignore",
    timeout: 5000,
    windowsHide: true,
  });
  return result.status === 0;
}

for (const command of candidates()) {
  if (!probe(command)) continue;
  const result = spawnSync(command, process.argv.slice(2), {
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) {
    console.error(`启动 Python 失败：${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

console.error(
  "未找到可用的 Python 解释器。请安装 Python，或设置 PYTHON 环境变量指向 python 可执行文件。",
);
process.exit(1);
