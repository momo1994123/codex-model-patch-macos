# codex-model-patch-macos

macOS 上修复 ChatGPT / Codex 桌面端 **模型选择器不显示自定义模型** 的补丁工具。

> 症状：使用官方 ChatGPT 账号登录时，composer 的模型 pill 正常显示模型名；
> 一旦切到自建 provider（中转、本地代理、自托管网关、`base_url`），pill 就退回
> `自定义` / `custom`，即使 `config.toml` 里 `model` 和模型目录都配好了。

本仓库是 [momo1994123/codex-model-patch](https://github.com/momo1994123/codex-model-patch) 的
macOS 修正版。上游工具在新版 App 上会**直接报错跑不起来**，而且它只解决了三个原因中的一个。

---

## 三个叠加的原因

修复前必须三个都处理，只做客户端补丁是不够的。

### 1. asar 头解析错误（上游工具在此版直接崩）

新版 Electron 会对 asar 的 JSON 头做填充。上游用 `16 + json_len` 计算数据区起点，
实际是 `8 + header_size`，差 3 字节：

```
AssertionError: (1594421, 1594424)
```

本仓库的解析器用 `8 + header_size`，实测在 ChatGPT.app 26.727.51351 上正确。

### 2. 压缩后的变量名变了（上游硬编码字符串搜不到）

上游硬编码查找 `(u?n.has(r.model):!r.hidden)`；新版是：

```js
function q$r({additionalAvailableModels:e,authMethod:t,availableModels:n,model:r,useHiddenModels:i}){
  return e?.has(r.model)===!0 || (i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)
}
```

当 `use_hidden_models` 为 true 且 `available_models` 白名单为空时，
这个 auth 分支会把所有模型丢掉。本工具用正则匹配分支结构、按站点计算等长填充，
两个版本的写法都能识别，原地改写为：

```js
(i&&t!==`amazonBedrock`?n.has(r.model):!r.hidden)
        ->
(!r.hidden/*  padding  */)
```

字节长度不变，并重算该条目受影响分块的 SHA256 写回头部，Electron 的
asar 完整性校验照过。

### 3. 模型压根不在服务端目录里（最关键）

客户端过滤器作用的对象是 CLI `model/list` 返回的列表。切到自定义 provider 后，
CLI **不会**把你 `config.toml` 里的 `model` 加进目录 —— 目录里仍然只有内置的
OpenAI 模型。列表里没有的东西，过滤器再怎么放行也显示不出来。

用 `codex debug models` 导出内置目录，合并进你的自定义模型，写到
`model_catalog_json` 指向的文件即可（见下）。

---

## 快速开始

需要 `python3` 和 `codesign`（Xcode Command Line Tools）。

### 步骤 1 — 让自定义模型进入目录

```bash
python3 build_model_catalog.py glm-5.3-flash "GLM-5.3-Flash"
```

它会导出内置目录、克隆模板条目、写入 `~/.codex/model-catalog.json`（默认），并打印
下一步要加的配置行。然后在 `~/.codex/config.toml` 的**顶层**（任何 `[table]` 之前）加：

```toml
model_catalog_json = "/Users/<you>/.codex/model-catalog.json"
```

验证（应能在列表里看到你的模型、`hidden` 为 `false`）：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex debug models | \
  python3 -c "import json,sys;print([m['slug'] for m in json.load(sys.stdin)['models']])"
```

### 步骤 2 — 打客户端补丁并重签名

```bash
./apply-patch-mac.sh
```

脚本会：退出 App → 打 asar 补丁 → ad-hoc 重签名 → 重新打开。

先干跑看看会改什么：

```bash
python3 codex_model_patch.py patch --dry-run
```

### 单独使用补丁工具

```bash
python3 codex_model_patch.py locate   [path/to/app.asar]
python3 codex_model_patch.py patch    [path/to/app.asar] [--dry-run]
python3 codex_model_patch.py restore  [path/to/app.asar]
```

不传路径时自动在常见安装位置查找 `app.asar`（也可用环境变量
`CODEX_ASAR_DIRS` 指定，`:` 分隔）。

---

## 为什么要重签名

改动 `app.asar` 会让 Apple 的封装资源签名失效，直接启动会：

```
SIGKILL (Code Signature Invalid)   termination namespace=CODESIGNING
```

首次用原 entitlements 做 ad-hoc 签名也会失败——原签名带着
`application-identifier` / `developer.team-identifier` / `aps-environment` /
`keychain-access-groups` 这些**受限** entitlement，ad-hoc 签名无法携带，系统在
spawn 阶段就把进程杀掉。

`entitlements.trimmed.plist` 只保留 App 真正需要的（JIT、未签名可执行内存、
关闭库校验、摄像头/麦克风、网络、用户选择文件、日历、AppleScript 自动化），
`apply-patch-mac.sh` 用它签名。

**代价**：去掉了 `keychain-access-groups` 和 `aps-environment` ——
推送通知可能不工作，钥匙串分组访问受限。本工具作者的场景里 auth 走
`~/.codex/auth.json`，不受影响。如果你需要这两个，得自己用开发者证书签名，
而不是 ad-hoc。

---

## 回滚

```bash
./restore.sh
```

它会从 `app.asar.bak_codexmodels` 恢复原始 asar，并重新 ad-hoc 签名（否则
改完的包签名已失效、启不来）。

**完全回滚到厂商签名**：需要在打补丁**之前**整包备份 App：

```bash
ditto /Applications/ChatGPT.app /path/to/ChatGPT.app.orig
```

恢复时把整包换回去即可（ad-hoc 签名无法还原成 OpenAI 的 Developer ID 签名）。

配置侧回滚：

- `~/.codex/model-catalog.json` 删掉或改名
- 从 `config.toml` 删掉 `model_catalog_json = ...` 行

---

## 注意事项

- **App 更新会覆盖 `app.asar`（连同 `.bak_codexmodels` 备份）**，
  之后需要重跑 `apply-patch-mac.sh`。`model_catalog_json` 与 `config.toml` 不受影响。
- 目标字节串/分支形状是**版本相关**的。`locate` 找不到分支会拒绝继续，
  不会盲改。升级 App 后如果 `locate` 报 `(no branch site...)`，说明压缩形态又变了，
  提 issue 附上 `locate` 输出。
- 工具只做等长原地替换 + 重算完整性哈希：不改文件大小、不重打包 asar、
  不引入外部依赖、不联网。

---

## English

Client-side fix for the Codex / ChatGPT desktop app on macOS where the composer
model pill shows `custom` instead of the real model name when a third-party
provider is configured.

Three stacked causes are handled: (1) an asar header offset bug that makes the
upstream tool crash on newer Electron builds, (2) version-specific minified
variable names that break the upstream hard-coded target string, and (3) the
custom model never being present in the CLI's `model/list` catalog at all —
fixed by generating a `model_catalog_json` catalog. The asar patch is an
equal-length in-place substitution with refreshed integrity hashes; the app is
then ad-hoc re-signed (see `entitlements.trimmed.plist` for why entitlements
must be trimmed).

## License

MIT — see [LICENSE](LICENSE).
