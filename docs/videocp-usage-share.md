# videocp 操作分享文档

面向对象：需要下载短视频、批量拉取账号主页视频，或把来源账号最新视频同步发布到 QQ 频道的同事。

建议分享时长：15-20 分钟。可以先演示 `doctor`、单视频下载、主页批量下载，再讲 `sync` 自动同步。

## 1. videocp 是什么

`videocp` 是一个 Python 命令行工具，用于从多个视频平台下载视频，并支持按配置把来源账号的新视频同步发布到 QQ 频道。

核心能力：

| 能力 | 说明 |
| --- | --- |
| 单视频下载 | 支持抖音、B 站、小红书、Instagram、YouTube 以及其他 `yt-dlp` 支持的网站 |
| 主页批量下载 | 支持抖音用户主页、B 站空间、小红书用户主页、Instagram reels、YouTube shorts/videos |
| 批量输入 | 支持命令行传多个链接，也支持从 txt 文件读取 |
| 链接清洗 | 能从复制分享文案里提取 URL，并转成规范链接 |
| QQ 频道同步 | 按 `tasks.yaml` 拉取最新视频、去重、下载并发布 |
| 浏览器登录复用 | 使用独立 Chrome profile 复用登录态，适合需要登录的网站 |

整体流程：

```mermaid
flowchart LR
    A["输入链接或账号主页"] --> B["解析并规范化 URL"]
    B --> C{"是否主页"}
    C -- "是" --> D["展开最新 N 条视频"]
    C -- "否" --> E["直接处理单视频"]
    D --> F["提取视频地址和元信息"]
    E --> F
    F --> G["下载 mp4 和 sidecar json"]
    G --> H{"是否 sync"}
    H -- "否" --> I["保存在 downloads 目录"]
    H -- "是" --> J["发布到 QQ 频道或其他目标"]
    J --> K["写入 sync_history 去重"]
```

## 2. 安装和环境准备

进入项目目录：

```bash
cd /Users/issaczhang/code/videocp
```

安装 Python 包和开发依赖：

```bash
python3 -m pip install -e '.[dev]'
```

如果本机没有全局安装，也可以使用项目里的虚拟环境运行：

```bash
.venv/bin/python -m videocp --help
```

推荐安装外部工具：

```bash
brew install ffmpeg yt-dlp
```

依赖说明：

| 工具 | 是否必须 | 用途 |
| --- | --- | --- |
| Chrome 或 Chromium 系浏览器 | 必须 | 登录态复用、CDP 抓取、B 站扫码登录、QQ 频道页面发布 |
| ffmpeg | 推荐 | HLS 下载、音视频合并、部分后处理 |
| yt-dlp | 推荐 | YouTube、Instagram、通用站点下载 |

第一次使用前，先在自己的常用浏览器中登录需要访问的平台，例如 B 站、抖音、小红书、Instagram、YouTube、QQ 频道。

## 3. 首次检查：doctor

`doctor` 用来检查浏览器、专用 profile、CDP、ffmpeg、yt-dlp 是否可用。

```bash
videocp doctor
```

如果当前 shell 里没有 `videocp` 命令，可以用：

```bash
.venv/bin/python -m videocp doctor
```

常见输出项：

| 检查项 | 含义 |
| --- | --- |
| `browser_detect` | 是否找到 Chrome 系浏览器 |
| `profile_seed` | 是否成功准备 videocp 专用浏览器 profile |
| `ffmpeg` | 是否找到 ffmpeg；缺失时部分下载和合并能力受影响 |
| `ytdlp` | 是否找到 yt-dlp；缺失时 YouTube/Instagram/通用站点受影响 |
| `cdp_startup` | 是否能启动并连接浏览器调试端口 |

如果需要看到浏览器窗口方便登录或扫码，使用 `--keep-open`。登录完成后回到终端按回车，浏览器会关闭并保存登录态。

```bash
videocp doctor --no-headless --keep-open
```

也可以直接打开指定站点：

```bash
videocp doctor --no-headless --keep-open \
  --login-url https://www.douyin.com/ \
  --login-url https://www.bilibili.com/ \
  --login-url https://www.xiaohongshu.com/ \
  --login-url https://pd.qq.com/
```

## 4. 单视频下载

最常用命令：

```bash
videocp download '<视频链接或分享文案>'
```

示例：

```bash
videocp download 'https://www.bilibili.com/video/BV1764y1y76G/'
videocp download 'https://www.douyin.com/video/1234567890'
videocp download 'https://www.xiaohongshu.com/explore/69be081c0000000021010b12?xsec_token=...'
videocp download 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
videocp download 'https://www.instagram.com/reel/DWQQpz5lLZD/'
```

抖音复制出来的分享文案也可以直接传：

```bash
videocp download '7.86 复制打开抖音，看看【示例】 https://v.douyin.com/xxxxxx/'
```

指定保存目录：

```bash
videocp download 'https://www.douyin.com/video/1234567890' --output-dir ./downloads
```

输出 JSON，适合脚本调用：

```bash
videocp download 'https://www.douyin.com/video/1234567890' --json
```

下载结果默认保存在：

```text
downloads/{site}-{author}/{content_id}.mp4
downloads/{site}-{author}/{content_id}.json
```

`.json` sidecar 里会记录来源 URL、作者、标题、候选下载地址、最终选择的下载地址等信息，排障时很有用。

## 5. 主页批量下载

传账号主页时，`videocp` 会先展开最新视频，再逐条下载。默认数量来自 `config.yaml` 的 `download.profile_videos_count`，当前示例配置是 3 条。

示例：

```bash
videocp download 'https://www.douyin.com/user/MS4wLjABAAAAxxxxxx'
videocp download 'https://space.bilibili.com/7612168'
videocp download 'https://www.xiaohongshu.com/user/profile/5756c80da9b2ed37b185c08e'
videocp download 'https://www.instagram.com/ddk69k/reels/'
videocp download 'https://www.youtube.com/@hackbearterry/shorts'
videocp download 'https://www.youtube.com/@hackbearterry/videos'
```

临时指定数量：

```bash
videocp download 'https://space.bilibili.com/7612168' --profile-videos-count 5
```

说明：

- 抖音主页会跳过置顶视频，只抓最近视频。
- B 站空间使用内置 TV 模式下载，第一次可能弹出扫码页，需要扫码一次。
- YouTube、Instagram 和其他通用站点依赖 `yt-dlp`，会尽量导出浏览器 cookie 用于登录态下载。

## 6. 多链接和批量文件

命令行直接传多个输入：

```bash
videocp download \
  'https://www.douyin.com/video/111' \
  'https://www.douyin.com/video/222'
```

把混合分享文案整理成标准链接列表：

```bash
videocp prepare-list \
  --output-file ./links.txt \
  'https://www.douyin.com/jingxuan?modal_id=7596491775800282387' \
  'https://www.bilibili.com/video/BV1764y1y76G/'
```

从文件批量下载：

```bash
videocp download --input-file ./links.txt
```

`links.txt` 支持一行一个链接或分享文案，空行和以 `#` 开头的注释行会被忽略。

## 7. 常用配置：config.yaml

`videocp` 会从当前目录向上查找 `config.yaml`。命令行参数会覆盖配置文件。

当前常用配置结构：

```yaml
download:
  output_dir: ./downloads
  max_concurrent: 3
  max_concurrent_per_site: 1
  start_interval_secs: 0
  profile_videos_count: 3

browser:
  profile_dir: ~/Library/Caches/videocp/chrome-profile
  browser_path: ""
  headless: true

request:
  timeout_secs: 30

watermark:
  enabled: false
  base_url: https://openrouter.ai/api/v1/chat/completions
  model: google/gemini-3-flash-preview
```

字段解释：

| 字段 | 用途 |
| --- | --- |
| `download.output_dir` | 下载文件保存目录 |
| `download.max_concurrent` | 总并发下载任务数 |
| `download.max_concurrent_per_site` | 单个平台并发数，建议保守设置 |
| `download.start_interval_secs` | 任务启动间隔，避免请求过密 |
| `download.profile_videos_count` | 主页默认抓取最新多少条 |
| `browser.profile_dir` | videocp 专用 Chrome profile 目录 |
| `browser.browser_path` | 指定 Chrome 路径；为空时自动探测 |
| `browser.headless` | 是否无头运行；登录和排障时建议设为 `false` 或传 `--no-headless` |
| `request.timeout_secs` | 页面解析和请求超时时间 |
| `watermark.enabled` | 是否启用 B 站水印识别和去除后处理 |

常用命令行覆盖：

```bash
videocp download '<url>' --output-dir ./tmp --no-headless --timeout-secs 60
videocp download '<profile-url>' --profile-videos-count 10
videocp download '<url>' --browser-path '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

## 8. 同步发布：video sync

`videocp` 同时提供 `video` 命令别名，下面两种写法等价：

```bash
videocp sync
video sync
```

同步流程：

1. 读取 `tasks.yaml`。
2. 展开每个任务的来源主页最新视频。
3. 查询 `sync_history.json`，跳过已处理内容。
4. 下载视频到 `downloads`。
5. 按 `publish_method` 发布。
6. 写入 `sync_history.json` 和 `sync_logs/YYYY-MM-DD.log`。

先做一次演练，不下载也不发布：

```bash
video sync --dry-run
```

只跑一个任务：

```bash
video sync --task-name douyin-example
```

每个任务临时只处理最新 1 条：

```bash
video sync --count 1
```

输出 JSON：

```bash
video sync --json
```

## 9. tasks.yaml 配置方式

最小示例：

```yaml
sync:
  history_file: ./sync_history.json
  skill_dir: ~/.claude/skills/tencent-channel-community/
  videos_per_task: 3
  publish_method: skill
  skip_rate: 0.2

tasks:
  - name: "douyin-example"
    source_url: "https://www.douyin.com/user/MS4wLjABAAAAxxxxxx"
    title_template: "{desc}"
```

字段解释：

| 字段 | 用途 |
| --- | --- |
| `sync.history_file` | 去重历史文件；已成功或已跳过的内容不会重复处理 |
| `sync.skill_dir` | `publish_method: skill` 使用的 QQ 频道发布 skill 路径 |
| `sync.videos_per_task` | 每个任务默认处理最新多少条 |
| `sync.publish_method` | 全局发布方式：`skill`、`cdp`、`youtube` |
| `sync.skip_rate` | 随机跳过概率；置顶视频不会随机跳过 |
| `tasks[].name` | 任务唯一名称，后续按它过滤和去重 |
| `tasks[].source_url` | 来源账号主页或单视频链接 |
| `tasks[].title_template` | 标题模板，支持 `{desc}`、`{title}`、`{author}`、`{site}`、`{content_id}` |
| `tasks[].content_template` | 正文模板，支持同样变量 |
| `tasks[].count` | 单任务覆盖 `videos_per_task` |
| `tasks[].publish_method` | 单任务覆盖全局发布方式 |
| `tasks[].skip_rate` | 单任务覆盖全局随机跳过概率 |

发布方式说明：

| 发布方式 | 适用场景 | 注意事项 |
| --- | --- | --- |
| `skill` | 使用本地 `tencent-channel-community` skill 发布 | 当前代码会按作者身份发布，`guild_id` / `channel_id` 会被忽略 |
| `cdp` | 打开真实 QQ 频道网页，用浏览器自动发布 | 需要 `guild_id`，需要浏览器已登录 QQ 频道 |
| `youtube` | 发布到 YouTube | 需要浏览器登录对应账号 |

`cdp` 示例：

```yaml
sync:
  history_file: ./sync_history.json
  videos_per_task: 1
  publish_method: cdp

tasks:
  - name: "bilibili-to-channel"
    source_url: "https://space.bilibili.com/7612168"
    guild_id: "657469764024457583"
    title_template: "{desc}"
```

## 10. 分享时推荐演示脚本

可以按下面顺序现场演示：

```bash
# 1. 看命令和环境
videocp --help
videocp doctor --no-headless --keep-open --login-url https://www.bilibili.com/

# 2. 单视频下载
videocp download 'https://www.bilibili.com/video/BV1764y1y76G/'

# 3. 分享文案清洗成链接列表
videocp prepare-list --output-file ./links.txt '<复制来的分享文案>'
cat ./links.txt

# 4. 文件批量下载
videocp download --input-file ./links.txt

# 5. 主页抓最新 N 条
videocp download 'https://space.bilibili.com/7612168' --profile-videos-count 3

# 6. 同步任务演练
video sync --dry-run --count 1
```

演示时建议先用 `--no-headless --keep-open`，让同事看到浏览器登录、扫码、页面发布的过程。稳定后再切回无头模式。

## 11. 常见问题和处理

| 现象 | 可能原因 | 处理方式 |
| --- | --- | --- |
| `No Chrome-family browser found` | 没找到 Chrome 系浏览器 | 安装 Chrome，或传 `--browser-path` |
| `doctor --no-headless` 窗口很快关闭 | `doctor` 默认只是连通性检查 | 使用 `videocp doctor --no-headless --keep-open`，登录完成后回到终端按回车 |
| `cdp_startup` 失败 | 浏览器无法启动或端口不可用 | 用 `--no-headless` 重试，检查浏览器路径和 profile 权限 |
| 抖音/小红书抓不到视频 | 未登录、页面风控、链接已失效 | 先在浏览器登录，再用 `--no-headless` 观察页面 |
| B 站第一次下载卡住 | TV 模式需要扫码授权 | 按弹出的二维码扫码一次，后续会缓存 token |
| YouTube/Instagram 下载失败 | `yt-dlp` 缺失或登录态不可用 | 安装 `yt-dlp`，确认浏览器已登录 |
| HLS 或音视频合并失败 | `ffmpeg` 缺失 | `brew install ffmpeg` |
| `download --input-file` 没有处理某行 | 空行或注释行会被忽略 | 检查 txt 中是否有真实 URL |
| `sync` 重复跳过 | `sync_history.json` 里已有记录 | 确认是否已经发布过；必要时只删除对应任务和内容的历史记录 |
| `sync` 找不到任务 | `--task-name` 和 `tasks.yaml` 不完全一致 | 复制 `tasks[].name` 的完整值 |

## 12. 安全和协作注意事项

- 不要把 `.env`、浏览器 profile、cookie、账号 token 发给同事或提交到仓库。
- `sync_history.json` 和 `sync_logs/` 可能包含发布记录、分享链接、视频路径，外发前先确认是否能公开。
- `downloads/` 里是实际视频文件，注意版权和内部传播范围。
- 给同事配置 `tasks.yaml` 时，建议先用 `video sync --dry-run --count 1` 确认任务会处理哪些内容。
- 第一次配置发布能力时，优先使用 `--no-headless` 跑通登录和权限，再切换到 `headless: true`。

## 13. 一句话总结

日常下载用：

```bash
videocp download '<链接或分享文案>'
```

主页批量用：

```bash
videocp download '<账号主页>' --profile-videos-count 5
```

自动同步发布用：

```bash
video sync --dry-run --count 1
video sync --count 1
```
